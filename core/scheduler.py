import asyncio
import logging
from datetime import datetime

logger = logging.getLogger("Scheduler")

def parse_field(field, min_val, max_val):
    """
    Parses a single cron field and returns a matcher function.
    Correctly binds loop variables to prevent late binding issues.
    """
    if field == '*':
        return lambda val: True

    parts = field.split(',')
    matchers = []
    for part in parts:
        if '/' in part:
            range_part, step_part = part.split('/')
            step = int(step_part)
            if step <= 0:
                raise ValueError(f"Invalid cron step: {part}")
            start = min_val
            end = max_val
            if range_part != '*':
                if '-' in range_part:
                    s, e = range_part.split('-')
                    start = int(s)
                    end = int(e)
                else:
                    start = int(range_part)
            # Use default arguments to capture start, end, and step during closure creation
            matchers.append(lambda val, s=start, e=end, st=step: val >= s and val <= e and (val - s) % st == 0)
        elif '-' in part:
            s, e = part.split('-')
            start = int(s)
            end = int(e)
            if start > end:
                raise ValueError(f"Invalid cron range: {part}")
            matchers.append(lambda val, s=start, e=end: val >= s and val <= e)
        else:
            exact = int(part)
            matchers.append(lambda val, ex=exact: val == ex)

    # Return a function checking if any matcher matches
    return lambda val: any(m(val) for m in matchers)

class Scheduler:
    def __init__(self):
        self.tasks = []
        self._task_loop_handle = None
        self.is_running = False

    def start(self):
        """
        Starts the scheduler background async loop.
        """
        if self.is_running:
            return
        self.is_running = True
        self._task_loop_handle = asyncio.create_task(self._loop())

    def stop(self):
        """
        Stops the scheduler and cancels the background task.
        """
        self.is_running = False
        if self._task_loop_handle:
            self._task_loop_handle.cancel()
            self._task_loop_handle = None

    def schedule(self, cron_expression, callback, name='task', timezone=None):
        """
        Registers a callback to execute on a cron schedule.
        Returns a cancel callable.
        """
        fields = cron_expression.strip().split()
        if len(fields) != 5:
            raise ValueError(f"Invalid cron expression: '{cron_expression}'. Must have exactly 5 fields.")

        try:
            min_matcher = parse_field(fields[0], 0, 59)
            hour_matcher = parse_field(fields[1], 0, 23)
            dom_matcher = parse_field(fields[2], 1, 31)
            month_matcher = parse_field(fields[3], 1, 12)
            dow_matcher = parse_field(fields[4], 0, 7) # 0 or 7 is Sunday
        except Exception as e:
            raise ValueError(f"Failed to parse cron expression '{cron_expression}': {e}")

        task_data = {
            "name": name,
            "cron_expression": cron_expression,
            "callback": callback,
            "timezone": timezone
        }

        def match(date):
            from zoneinfo import ZoneInfo
            if timezone:
                try:
                    tz = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
                    # Convert naive date (system time) to system timezone-aware, then convert to target tz
                    if date.tzinfo is None:
                        system_now_aware = date.astimezone()
                        local_date = system_now_aware.astimezone(tz)
                    else:
                        local_date = date.astimezone(tz)
                except Exception as e:
                    logger.error(f"Failed to convert date to timezone '{timezone}': {e}")
                    local_date = date
            else:
                local_date = date

            minute = local_date.minute
            hour = local_date.hour
            dom = local_date.day
            month = local_date.month
            dow = local_date.isoweekday() # 1 = Monday, 7 = Sunday
            
            # Normalize Sunday as both 0 and 7
            dow_val = 0 if dow == 7 else dow
            is_dow_match = dow_matcher(dow_val) or (dow_val == 0 and dow_matcher(7)) or (dow_val == 7 and dow_matcher(0))

            return (min_matcher(minute) and
                    hour_matcher(hour) and
                    dom_matcher(dom) and
                    month_matcher(month) and
                    is_dow_match)

        task_data["match"] = match
        self.tasks.append(task_data)

        def cancel():
            if task_data in self.tasks:
                self.tasks.remove(task_data)
        return cancel

    async def _loop(self):
        """
        Background tick loop aligning to the start of each minute.
        """
        last_ticked_minute = None
        while self.is_running:
            now = datetime.now()
            # Calculate time left until the start of the next minute plus a 0.1s safety buffer
            seconds_to_next_minute = 60 - now.second - (now.microsecond / 1000000.0) + 0.1
            try:
                await asyncio.sleep(seconds_to_next_minute)
            except asyncio.CancelledError:
                break

            if not self.is_running:
                break

            now_wake = datetime.now()
            minute_key = (now_wake.year, now_wake.month, now_wake.day, now_wake.hour, now_wake.minute)
            if minute_key != last_ticked_minute:
                last_ticked_minute = minute_key
                self._tick()

    def _tick(self):
        """
        Evaluates and runs all scheduled tasks matching the current time.
        """
        now = datetime.now()
        for task in list(self.tasks):
            if task["match"](now):
                # Compare system timezone/time and actual local timezone/time
                system_tz = datetime.now().astimezone().tzinfo
                system_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                target_tz_str = "System Default"
                local_time_str = system_time_str
                
                if task.get("timezone"):
                    from zoneinfo import ZoneInfo
                    try:
                        tz = ZoneInfo(task["timezone"]) if isinstance(task["timezone"], str) else task["timezone"]
                        target_tz_str = str(tz)
                        local_time_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass
                
                logger.info(
                    f"[Scheduler] Triggering task '{task['name']}' (Cron: '{task['cron_expression']}'). "
                    f"Timezone Comparison -> System: {system_tz} (Time: {system_time_str}) | "
                    f"Actual Local: {target_tz_str} (Time: {local_time_str})"
                )
                
                try:
                    if asyncio.iscoroutinefunction(task["callback"]):
                        asyncio.create_task(self._safe_run_async(task["callback"], task["name"]))
                    else:
                        task["callback"]()
                except Exception as e:
                    logger.error(f"Error running scheduled task '{task['name']}': {e}", exc_info=True)

    async def _safe_run_async(self, callback, name):
        try:
            await callback()
        except Exception as e:
            logger.error(f"Error running async scheduled task '{name}': {e}", exc_info=True)
