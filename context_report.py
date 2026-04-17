#!/usr/bin/env python3
"""
context_report.py — Let Claude see its own context window usage.

Reads the current session's transcript JSONL file to extract token usage
from each API turn, then reports context window consumption.

Usage:
    python3 context_report.py                    # auto-detect current project session
    python3 context_report.py /path/to/transcript.jsonl  # specific transcript
    python3 context_report.py --session-id <uuid>        # by session ID
"""

import json
import logging
import os
import shlex
import sys
import glob
import subprocess
import platform
import time
from pathlib import Path

# Logging setup — writes to ~/.cache/context-report/debug.log
_log_dir = Path.home() / ".cache" / "context-report"
_log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_log_dir / "debug.log"),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("context_report")


def _shell_quote(s):
    """Quote a string for safe embedding in a shell command."""
    return shlex.quote(s)


def find_transcript(args):
    """Find the transcript file to analyze."""
    # Filter out flags like --json
    positional = [a for a in args[1:] if not a.startswith("--")]
    flags = [a for a in args[1:] if a.startswith("--")]

    if "--session-id" in flags and positional:
        session_id = positional[0]
        base = Path.home() / ".claude" / "projects"
        for jsonl in base.rglob(f"{session_id}.jsonl"):
            return str(jsonl)
        print(f"No transcript found for session {session_id}", file=sys.stderr)
        sys.exit(1)
    elif positional:
        path = positional[0]
        if os.path.isfile(path):
            return path
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    # Auto-detect: find the most recently modified transcript across all projects
    base = Path.home() / ".claude" / "projects"
    all_transcripts = list(base.rglob("*.jsonl"))
    if not all_transcripts:
        print("No transcript files found in ~/.claude/projects/", file=sys.stderr)
        sys.exit(1)

    return str(max(all_transcripts, key=lambda p: p.stat().st_mtime))


def parse_transcript(path):
    """Parse a transcript JSONL and extract per-turn token usage."""
    turns = []
    session_id = None
    model = None

    with open(path) as f:
        for line in f:
            entry = json.loads(line)

            if not session_id and entry.get("sessionId"):
                session_id = entry["sessionId"]

            if entry.get("type") != "assistant":
                continue

            msg = entry.get("message", {})
            usage = msg.get("usage", {})
            if not usage:
                continue

            if not model and msg.get("model"):
                model = msg["model"]

            input_tokens = usage.get("input_tokens", 0)
            cache_creation = usage.get("cache_creation_input_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            total_input = input_tokens + cache_creation + cache_read

            turns.append({
                "input_tokens": input_tokens,
                "cache_creation": cache_creation,
                "cache_read": cache_read,
                "output_tokens": output_tokens,
                "total_input": total_input,
            })

    return turns, session_id, model


def get_context_window_size(model, turns=None):
    """Return context window size, detecting 1M variants from multiple signals.

    Signal priority (first match wins):
      1. CLAUDE_CONTEXT_WINDOW env var (explicit override)
      2. [1m] suffix in model name (future-proof if the transcript preserves it)
      3. Legacy 4.6 family (was 1M before the [1m] convention existed)
      4. Observed-usage heuristic (any past turn > 200k implies 1M)
      5. Default 200k

    The Anthropic API strips the [1m] suffix before writing the model field to
    the transcript, so opus-4-7[1m] sessions arrive here as plain 'claude-opus-4-7'.
    The env var and heuristic together cover that case.
    """
    override = os.environ.get("CLAUDE_CONTEXT_WINDOW")
    if override:
        try:
            return int(override)
        except ValueError:
            pass

    if model:
        m = model.lower()
        if "[1m]" in m:
            return 1_048_576
        if ("opus" in m or "sonnet" in m) and ("4-6" in m or "4.6" in m):
            return 1_048_576

    if turns:
        max_input = max((t.get("total_input", 0) for t in turns), default=0)
        if max_input > 200_000:
            return 1_048_576

    return 200_000


def deduplicate_turns(turns):
    """Remove duplicate log entries (Claude Code logs each turn twice)."""
    seen = []
    for t in turns:
        key = (t["total_input"], t["output_tokens"])
        if not seen or seen[-1] != key:
            seen.append(key)
        else:
            continue
        yield t


def report(path):
    """Generate and print the context report."""
    turns, session_id, model = parse_transcript(path)
    if not turns:
        print("No assistant turns with usage data found.")
        return

    unique_turns = list(deduplicate_turns(turns))
    context_size = get_context_window_size(model, unique_turns)

    # Latest turn = current context state
    latest = unique_turns[-1]
    current_context = latest["total_input"]
    used_pct = (current_context / context_size) * 100

    # Cumulative stats
    total_output_all = sum(t["output_tokens"] for t in unique_turns)
    total_cache_hits = sum(t["cache_read"] for t in unique_turns)
    total_cache_creates = sum(t["cache_creation"] for t in unique_turns)
    cache_hit_rate = (total_cache_hits / (total_cache_hits + total_cache_creates) * 100
                      if (total_cache_hits + total_cache_creates) > 0 else 0)

    # Growth: how much context grew from first to last
    first = unique_turns[0]
    growth = current_context - first["total_input"]

    print(f"{'=' * 50}")
    print(f"  CONTEXT WINDOW SELF-REPORT")
    print(f"{'=' * 50}")
    print(f"  Session:  {session_id or 'unknown'}")
    print(f"  Model:    {model or 'unknown'}")
    print(f"  Turns:    {len(unique_turns)}")
    print()
    print(f"  Current context (latest turn input):")
    print(f"    Uncached input:     {latest['input_tokens']:>10,}")
    print(f"    Cache creation:     {latest['cache_creation']:>10,}")
    print(f"    Cache read:         {latest['cache_read']:>10,}")
    print(f"    {'─' * 36}")
    print(f"    Total input:        {current_context:>10,}")
    print(f"    Latest output:      {latest['output_tokens']:>10,}")
    print()
    print(f"  Context window:       {context_size:>10,}")
    print(f"  Used:                 {current_context:>10,}  ({used_pct:.1f}%)")
    print(f"  Remaining:            {context_size - current_context:>10,}  ({100 - used_pct:.1f}%)")
    print()

    # Visual bar
    bar_width = 40
    filled = int(bar_width * used_pct / 100)
    bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
    print(f"  [{bar}] {used_pct:.1f}%")
    print()

    print(f"  Session stats:")
    print(f"    Total output generated:  {total_output_all:>10,} tokens")
    print(f"    Context growth:          {growth:>10,} tokens")
    print(f"    Cache hit rate:          {cache_hit_rate:>9.1f}%")
    print()

    # Status message
    if used_pct > 90:
        print(f"  !! Context nearly full — compression imminent")
    elif used_pct > 75:
        print(f"  !  Getting full — consider wrapping up or starting fresh")
    elif used_pct > 50:
        print(f"  ~  Moderate usage — room for more work")
    else:
        print(f"     Plenty of headroom")

    print(f"{'=' * 50}")

    # Machine-readable JSON output with --json flag
    if "--json" in sys.argv:
        print()
        print(json.dumps({
            "session_id": session_id,
            "model": model,
            "turns": len(unique_turns),
            "context_window_size": context_size,
            "current_context_tokens": current_context,
            "used_percentage": round(used_pct, 1),
            "remaining_tokens": context_size - current_context,
            "latest_output_tokens": latest["output_tokens"],
            "total_output_tokens": total_output_all,
            "cache_hit_rate": round(cache_hit_rate, 1),
        }, indent=2))


def get_state_file(session_id):
    """Get path to threshold state file for this session."""
    state_dir = Path.home() / ".cache" / "context-report"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{session_id}.json"


def load_alerted_thresholds(session_id):
    """Load which thresholds have already fired for this session."""
    state_file = get_state_file(session_id)
    if state_file.exists():
        try:
            return set(json.loads(state_file.read_text()).get("alerted", []))
        except (json.JSONDecodeError, KeyError):
            pass
    return set()


def save_alerted_threshold(session_id, threshold):
    """Record that a threshold has fired."""
    alerted = load_alerted_thresholds(session_id)
    alerted.add(threshold)
    state_file = get_state_file(session_id)
    state_file.write_text(json.dumps({"alerted": sorted(alerted)}))


def hook_mode(path):
    """Run in hook mode: output JSON for Claude Code's Stop hook.

    Only outputs when crossing a threshold for the first time.
    Thresholds: 40%, 50%, 60% (action required).

    Also caches the TTY on every invocation so self-clear can find it
    reliably later (hooks always run from main Claude's process tree).
    """
    # Cache TTY early — this runs from Claude's direct process tree,
    # so find_claude_tty() works reliably here.
    log.info("hook_mode: starting for transcript %s", path)
    tty = find_claude_tty()
    if tty:
        cache_tty(tty)
        log.debug("hook_mode: cached TTY %s", tty)
    else:
        log.warning("hook_mode: could not find claude TTY from process tree")

    turns, session_id, model = parse_transcript(path)
    if not turns:
        return

    unique_turns = list(deduplicate_turns(turns))
    context_size = get_context_window_size(model, unique_turns)
    latest = unique_turns[-1]
    current_context = latest["total_input"]
    used_pct = (current_context / context_size) * 100
    remaining = context_size - current_context

    alerted = load_alerted_thresholds(session_id or "unknown")

    # Define thresholds and their messages
    # Granular awareness at every 10%, actionable at 50%+, critical at 60%
    thresholds = [
        (10, "context-10",
         None,
         f"Context check-in: {used_pct:.0f}% ({current_context:,} / {context_size:,} tokens). "
         f"Turns: {len(unique_turns)}. Plenty of room."),
        (20, "context-20",
         None,
         f"Context check-in: {used_pct:.0f}% ({current_context:,} / {context_size:,} tokens). "
         f"Turns: {len(unique_turns)}. Healthy range."),
        (30, "context-30",
         None,
         f"Context check-in: {used_pct:.0f}% ({current_context:,} / {context_size:,} tokens). "
         f"Turns: {len(unique_turns)}. Still in the sweet spot for quality work."),
        (40, "context-40",
         None,
         f"Context check-in: {used_pct:.0f}% ({current_context:,} / {context_size:,} tokens). "
         f"Turns: {len(unique_turns)}. Remaining: ~{remaining:,} tokens. "
         f"Start being mindful of context growth. Avoid unnecessary tool output. "
         f"EARLY RESET AVAILABLE: If you can see a large task ahead that will consume "
         f"significant context (big codebase exploration, multi-file refactor, lengthy debugging), "
         f"you may choose to reset now while context is fresh. To do so: "
         f"1) Write handoff notes to .remember/now.md, "
         f"2) Run: python3 context_report.py --set-addendum '<your context>', "
         f"3) Run: python3 context_report.py --graceful-reset. "
         f"Only do this if the next task is genuinely large. If work is flowing well, continue."),
        (50, "context-50",
         f"Context: {used_pct:.0f}%",
         f"Context at {used_pct:.0f}% ({current_context:,} / {context_size:,} tokens). "
         f"Turns: {len(unique_turns)}. Remaining: ~{remaining:,} tokens. "
         f"DECISION POINT — you have two paths: "
         f"PATH A (reset now): If there is heavy work ahead, or you're at a natural stopping point, "
         f"or quality might degrade with more context, reset now. Steps: "
         f"1) Write handoff notes to .remember/now.md, "
         f"2) Run: python3 context_report.py --set-addendum '<your context>', "
         f"3) Run: python3 context_report.py --graceful-reset. "
         f"PATH B (continue): If current work is almost done or remaining tasks are small, "
         f"continue to 60% where auto-reset will handle it. "
         f"Evaluate your remaining task list and the complexity of what's next. Choose wisely."),
        (60, "context-60",
         f"Context at {used_pct:.0f}% — save and reset",
         f"CONTEXT LIMIT REACHED: {used_pct:.0f}% ({current_context:,} / {context_size:,} tokens). "
         f"Turns: {len(unique_turns)}. Remaining: ~{remaining:,} tokens. "
         f"ACTION REQUIRED — you MUST save and reset NOW. "
         f"1) Update any in-progress tasks with status notes. "
         f"2) Save key learnings and decisions to memory files. "
         f"3) Write a handoff summary to .remember/now.md with: what was done, what's next, any blockers, and key file paths. "
         f"4) Write a session addendum by running: "
         f"python3 context_report.py --set-addendum '<current focus, critical state, immediate next step>'. "
         f"This gets injected into the prompt that wakes up the next session — make it count. "
         f"After you respond, auto-clear will fire automatically. "
         f"Do this BEFORE continuing any other work."),
    ]

    # Find the highest NEW threshold crossed
    output = None
    triggered_key = None
    for pct, key, user_msg, claude_msg in thresholds:
        if used_pct >= pct and key not in alerted:
            save_alerted_threshold(session_id or "unknown", key)
            triggered_key = key
            output = {
                "systemMessage": claude_msg
            }
            if user_msg:
                output["systemMessage"] = user_msg + "\n\n" + claude_msg

    if output:
        print(json.dumps(output))

    # At 60%, after Claude has been told to save state,
    # schedule a self-clear if auto-clear is enabled.
    # This fires on the NEXT stop after 60% (giving Claude one turn to save).
    if "context-60" in alerted and "context-60-cleared" not in alerted:
        # Check if Claude was already told to save (60 was already alerted in a prior turn)
        # and this is a subsequent stop (meaning Claude has had a chance to save)
        if triggered_key != "context-60":
            cleared = trigger_self_clear_if_enabled(session_id or "unknown")
            if cleared:
                save_alerted_threshold(session_id or "unknown", "context-60-cleared")
                # Tell the user what's happening
                clear_output = {
                    "systemMessage": "Auto-clear triggered — waiting for idle, then /clear. Self-clear has been scheduled. It will wait for you to finish generating output, then type /clear, wait for the clear to process, then type the resume prompt. Your state should already be saved from the previous turn."
                }
                print(json.dumps(clear_output))


def find_claude_tty():
    """Walk up the process tree to find the TTY Claude Code is attached to.

    Looks for any process whose command ends with 'claude' (handles both
    'claude' and full-path variants), then returns the TTY of claude's
    parent shell (which is the terminal's real TTY).
    """
    log.debug("find_claude_tty: starting from PID %d", os.getpid())
    try:
        pid = os.getpid()
        for i in range(10):  # max 10 levels up
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,tty=,comm="],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                log.debug("  level %d: ps failed for PID %d (rc=%d)", i, pid, result.returncode)
                break
            raw = result.stdout.strip()
            parts = raw.split()
            log.debug("  level %d: PID %d -> raw=%r parts=%r", i, pid, raw, parts)
            if len(parts) < 2:
                log.debug("  level %d: too few parts, stopping", i)
                break
            ppid = parts[0]
            # comm= is the last field and may contain path separators
            comm = parts[2] if len(parts) >= 3 else ""
            comm_name = comm.rsplit("/", 1)[-1]  # basename
            log.debug("  level %d: comm=%r comm_name=%r", i, comm, comm_name)
            if comm_name == "claude":
                tty = parts[1] if len(parts) >= 2 else ""
                log.debug("  level %d: FOUND claude! tty=%r", i, tty)
                if tty and tty != "??":
                    result_tty = f"/dev/{tty}"
                    log.debug("  -> returning claude's own TTY: %s", result_tty)
                    return result_tty
                # Claude's TTY might be ??, check parent
                parent_info = subprocess.run(
                    ["ps", "-p", ppid, "-o", "tty="],
                    capture_output=True, text=True
                )
                parent_tty = parent_info.stdout.strip()
                log.debug("  level %d: claude's parent (PID %s) tty=%r", i, ppid, parent_tty)
                if parent_tty and parent_tty != "??":
                    result_tty = f"/dev/{parent_tty}"
                    log.debug("  -> returning parent TTY: %s", result_tty)
                    return result_tty
            pid = int(ppid)
    except (ValueError, IndexError, FileNotFoundError) as e:
        log.exception("find_claude_tty: exception: %s", e)
    log.debug("find_claude_tty: no TTY found, returning None")
    return None


def get_cached_tty():
    """Read the cached TTY path, if available."""
    cache_file = Path.home() / ".cache" / "context-report" / "tty-cache"
    if cache_file.exists():
        tty = cache_file.read_text().strip()
        log.debug("get_cached_tty: file exists, contents=%r, device_exists=%s", tty, os.path.exists(tty) if tty else False)
        if tty and os.path.exists(tty):
            return tty
    else:
        log.debug("get_cached_tty: no cache file at %s", cache_file)
    return None


def cache_tty(tty_path):
    """Cache the TTY path so self-clear can find it reliably."""
    cache_file = Path.home() / ".cache" / "context-report" / "tty-cache"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(tty_path)
    log.debug("cache_tty: wrote %r to %s", tty_path, cache_file)


def get_resumption_prompt():
    """Build the full resumption prompt from two layers:

    Layer 1 — Base prompt (always present):
      Custom override from ~/.cache/context-report/resume-prompt.txt,
      or a built-in default. This is the permanent foundation that tells
      Claude how to orient itself after a clear.

    Layer 2 — Session addendum (optional, one-shot):
      Written by Claude during the save-state turn at 60% to
      ~/.cache/context-report/resume-addendum.txt.
      Contains session-specific context: what was being worked on,
      key decisions, critical state, what to do next.
      CONSUMED AFTER USE — deleted once read so it never leaks
      into a future unrelated session.

    The final prompt is: base + addendum (if present).
    """
    cache_dir = Path.home() / ".cache" / "context-report"

    # Layer 1: Base prompt
    custom_base = cache_dir / "resume-prompt.txt"
    if custom_base.exists():
        base = custom_base.read_text().strip()
        if not base:
            base = None
    else:
        base = None

    if not base:
        base = (
            "Resuming after auto-clear. "
            "Read .remember/now.md for session handoff notes. "
            "Check memory files in the project memory directory for broader context. "
            "Then continue with the next task from where the previous session left off. "
            "Summarize what you found in the handoff notes before proceeding."
        )
    log.debug("get_resumption_prompt: base prompt (len=%d)", len(base))

    # Layer 2: Session addendum (consumed after read)
    addendum_file = cache_dir / "resume-addendum.txt"
    addendum = ""
    if addendum_file.exists():
        addendum = addendum_file.read_text().strip()
        if addendum:
            log.info("get_resumption_prompt: found session addendum (len=%d), will consume", len(addendum))
            # Consume — delete so it doesn't carry into future sessions
            addendum_file.unlink()
        else:
            addendum = ""
            addendum_file.unlink(missing_ok=True)

    # Combine
    if addendum:
        full = f"{base} Additionally: {addendum}"
    else:
        full = base

    log.debug("get_resumption_prompt: final prompt (len=%d)", len(full))
    return full


def write_session_addendum(text):
    """Write a session-specific addendum to the resumption prompt.

    Called by Claude (via --set-addendum) during the save-state turn at 60%.
    This gives the next session targeted context about what was happening.
    """
    addendum_file = Path.home() / ".cache" / "context-report" / "resume-addendum.txt"
    addendum_file.parent.mkdir(parents=True, exist_ok=True)
    addendum_file.write_text(text.strip())
    log.info("write_session_addendum: wrote %d chars to %s", len(text.strip()), addendum_file)


def _build_terminal_focus_applescript(claude_tty):
    """Build an AppleScript that finds and focuses the correct Terminal.app window."""
    tty_match = claude_tty or ""
    tty_search_block = ""
    if tty_match:
        tty_search_block = f'''
                repeat with w in windows
                    try
                        set t to selected tab of w
                        set tabTTY to tty of t
                        if tabTTY is "{tty_match}" then
                            set targetWindow to w
                            set targetTab to t
                            exit repeat
                        end if
                    end try
                end repeat
        '''
    return f'''
        tell application "System Events"
            set origApp to name of first process whose frontmost is true
        end tell
        tell application "Terminal"
            set targetWindow to missing value
            set targetTab to missing value
            {tty_search_block}
            if targetWindow is missing value then
                repeat with w in windows
                    if name of w contains "claude" then
                        set targetWindow to w
                        exit repeat
                    end if
                end repeat
            end if
            if targetWindow is missing value then
                return "no matching window found"
            end if
            set frontmost of targetWindow to true
        end tell
        delay 0.5
        tell application "System Events"
            set frontApp to name of first process whose frontmost is true
            if frontApp is not "Terminal" then
                return "terminal not frontmost, aborting"
            end if
        end tell
        return "ok"
    '''


def _build_keystroke_applescript(text):
    """Build a small AppleScript that types text + Enter into the frontmost app."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'''
        tell application "System Events"
            keystroke "{escaped}"
            delay 0.2
            keystroke return
        end tell
        return "ok"
    '''


def _build_idle_wait_shell(phase_label, stale_seconds=3, max_wait=30):
    """Build a shell snippet that blocks until Claude is idle (at the prompt).

    Uses transcript file staleness: the JSONL transcript is actively written
    during responses and goes quiet when Claude is waiting for input.
    When the file's mtime hasn't changed for `stale_seconds`, Claude is idle.

    Falls back to a fixed delay if no transcript is found (e.g. after /clear
    creates a new session before the new transcript exists).

    Previous approach used CPU% polling on the claude process, but MCP server
    connections keep Node.js active, so CPU never drops below threshold.
    """
    projects_dir = str(Path.home() / ".claude" / "projects")
    return f'''
# --- Wait for Claude to be idle ({phase_label}) ---
TRANSCRIPT=$(ls -t {_shell_quote(projects_dir)}/*/*.jsonl 2>/dev/null | head -1)
if [ -n "$TRANSCRIPT" ]; then
    echo "idle_wait({phase_label}): watching transcript $TRANSCRIPT"
    STALE_COUNT=0
    WAITED=0
    LAST_MTIME=$(stat -f %m "$TRANSCRIPT" 2>/dev/null || echo 0)
    while [ $STALE_COUNT -lt {stale_seconds} ] && [ $WAITED -lt {max_wait} ]; do
        sleep 1
        WAITED=$((WAITED + 1))
        # Re-check which transcript is newest — after /clear, a new one appears
        NEW_TRANSCRIPT=$(ls -t {_shell_quote(projects_dir)}/*/*.jsonl 2>/dev/null | head -1)
        if [ "$NEW_TRANSCRIPT" != "$TRANSCRIPT" ] && [ -n "$NEW_TRANSCRIPT" ]; then
            echo "idle_wait({phase_label}): transcript switched to $NEW_TRANSCRIPT"
            TRANSCRIPT=$NEW_TRANSCRIPT
            STALE_COUNT=0
            LAST_MTIME=$(stat -f %m "$TRANSCRIPT" 2>/dev/null || echo 0)
            continue
        fi
        CURR_MTIME=$(stat -f %m "$TRANSCRIPT" 2>/dev/null || echo 0)
        if [ "$CURR_MTIME" = "$LAST_MTIME" ]; then
            STALE_COUNT=$((STALE_COUNT + 1))
        else
            STALE_COUNT=0
            LAST_MTIME=$CURR_MTIME
        fi
    done
    echo "idle_wait({phase_label}): waited=${{WAITED}}s stale_count=$STALE_COUNT transcript=$TRANSCRIPT"
else
    echo "idle_wait({phase_label}): no transcript found, using fixed delay"
    sleep {stale_seconds}
fi
'''


def schedule_self_clear(delay_seconds=4):
    """Spawn a background process that types /clear into the correct terminal,
    then types a resumption prompt to continue work automatically.

    Two-phase approach with idle detection (fixes race condition where
    keystrokes were sent while Claude was still generating output):

    Phase 1 — Send /clear:
      1. Initial delay
      2. Poll CPU until Claude process is idle (not generating)
      3. Focus the correct Terminal window
      4. Type /clear + Enter

    Phase 2 — Send resume prompt:
      5. Poll CPU again until Claude is idle (clear has been processed)
      6. Re-focus the Terminal window (user may have switched away)
      7. Type the resumption prompt + Enter
      8. Restore original app focus

    Only one clear can be queued at a time (lock file prevents duplicates).
    Only works on macOS. Supports Terminal.app, iTerm2, and Warp.
    """
    log.info("schedule_self_clear: called with delay=%d", delay_seconds)

    if platform.system() != "Darwin":
        log.warning("schedule_self_clear: not macOS, aborting")
        return False

    # Prevent multiple queued clears
    lock_file = Path.home() / ".cache" / "context-report" / "clear-pending"
    if lock_file.exists():
        log.warning("schedule_self_clear: lock file exists, skipping (another clear already queued)")
        return False  # already queued
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch()
    log.debug("schedule_self_clear: lock file created at %s", lock_file)

    # Step 1: Use cached TTY (reliable), fall back to live lookup
    claude_tty = get_cached_tty()
    log.debug("schedule_self_clear: cached TTY = %r", claude_tty)
    if not claude_tty:
        claude_tty = find_claude_tty()
        log.debug("schedule_self_clear: live TTY lookup = %r", claude_tty)

    term_program = os.environ.get("TERM_PROGRAM", "")
    log.debug("schedule_self_clear: TERM_PROGRAM=%r, claude_tty=%r", term_program, claude_tty)
    lock_path_str = str(lock_file)

    # Get the resumption prompt
    resume_prompt = get_resumption_prompt()
    resume_escaped = resume_prompt.replace("\\", "\\\\").replace('"', '\\"')
    log.debug("schedule_self_clear: resume_prompt=%r", resume_prompt)

    clear_settle_delay = 5
    log_path = str(_log_dir / "self-clear.log")

    if "iTerm" in term_program:
        # iTerm2: 'write text' goes directly to session stdin — no keystroke race.
        # Still add idle detection before sending /clear.
        idle_wait_1 = _build_idle_wait_shell("phase1-before-clear")
        idle_wait_2 = _build_idle_wait_shell("phase2-after-clear", stale_seconds=5, max_wait=30)
        applescript_clear = f'''
            tell application "iTerm"
                set targetSession to missing value
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if tty of s is "{claude_tty}" then
                                set targetSession to s
                                exit repeat
                            end if
                        end repeat
                        if targetSession is not missing value then exit repeat
                    end repeat
                    if targetSession is not missing value then exit repeat
                end repeat
                if targetSession is missing value then
                    set targetSession to current session of current window
                end if
                tell targetSession to write text "/clear"
            end tell
        '''
        applescript_resume = f'''
            tell application "iTerm"
                set targetSession to missing value
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if tty of s is "{claude_tty}" then
                                set targetSession to s
                                exit repeat
                            end if
                        end repeat
                        if targetSession is not missing value then exit repeat
                    end repeat
                    if targetSession is not missing value then exit repeat
                end repeat
                if targetSession is missing value then
                    set targetSession to current session of current window
                end if
                tell targetSession to write text "{resume_escaped}"
            end tell
        '''
        shell_cmd = f'''#!/bin/bash
exec >>{_shell_quote(log_path)} 2>&1
echo "=== self-clear start $(date) ==="
echo "phase 0: initial delay {delay_seconds}s"
sleep {delay_seconds}

echo "phase 1: waiting for idle before /clear"
{idle_wait_1}

echo "phase 1: sending /clear via iTerm2"
osascript -e {_shell_quote(applescript_clear)}
echo "phase 1: /clear sent, exit=$?"

echo "phase 2: settle delay {clear_settle_delay}s"
sleep {clear_settle_delay}

echo "phase 2: waiting for idle before resume"
{idle_wait_2}

echo "phase 2: sending resume prompt via iTerm2"
osascript -e {_shell_quote(applescript_resume)}
echo "phase 2: resume sent, exit=$?"

rm -f {_shell_quote(lock_path_str)}
echo "=== self-clear complete $(date) ==="
'''
        log.debug("schedule_self_clear: using iTerm2 two-phase path")

    elif "WarpTerminal" in term_program:
        # Warp: keystroke-based, add idle detection
        idle_wait_1 = _build_idle_wait_shell("phase1-before-clear")
        idle_wait_2 = _build_idle_wait_shell("phase2-after-clear", stale_seconds=5, max_wait=30)
        as_clear = _build_keystroke_applescript("/clear")
        as_resume = _build_keystroke_applescript(resume_prompt)
        shell_cmd = f'''#!/bin/bash
exec >>{_shell_quote(log_path)} 2>&1
echo "=== self-clear start $(date) ==="
sleep {delay_seconds}

echo "phase 1: waiting for idle"
{idle_wait_1}

echo "phase 1: focusing Warp + sending /clear"
osascript -e 'tell application "System Events" to set frontmost of process "Warp" to true'
sleep 0.5
osascript -e {_shell_quote(as_clear)}
echo "phase 1: /clear sent, exit=$?"

sleep {clear_settle_delay}

echo "phase 2: waiting for idle"
{idle_wait_2}

echo "phase 2: focusing Warp + sending resume"
osascript -e 'tell application "System Events" to set frontmost of process "Warp" to true'
sleep 0.5
osascript -e {_shell_quote(as_resume)}
echo "phase 2: resume sent, exit=$?"

rm -f {_shell_quote(lock_path_str)}
echo "=== self-clear complete $(date) ==="
'''
        log.debug("schedule_self_clear: using Warp two-phase path")

    else:
        # Terminal.app — two-phase approach with idle detection between steps
        idle_wait_1 = _build_idle_wait_shell("phase1-before-clear")
        idle_wait_2 = _build_idle_wait_shell("phase2-after-clear", stale_seconds=5, max_wait=30)
        as_focus = _build_terminal_focus_applescript(claude_tty)
        as_clear = _build_keystroke_applescript("/clear")
        as_resume = _build_keystroke_applescript(resume_prompt)
        shell_cmd = f'''#!/bin/bash
exec >>{_shell_quote(log_path)} 2>&1
echo "=== self-clear start $(date) ==="
echo "phase 0: initial delay {delay_seconds}s"
sleep {delay_seconds}

# Phase 1: Wait for idle, then send /clear
echo "phase 1: waiting for Claude to be idle before /clear"
{idle_wait_1}

echo "phase 1: focusing Terminal window"
FOCUS_RESULT=$(osascript -e {_shell_quote(as_focus)})
echo "phase 1: focus result=$FOCUS_RESULT"
if [ "$FOCUS_RESULT" != "ok" ]; then
    echo "phase 1: ABORTING — could not focus terminal"
    rm -f {_shell_quote(lock_path_str)}
    exit 1
fi

echo "phase 1: typing /clear"
osascript -e {_shell_quote(as_clear)}
echo "phase 1: /clear sent, exit=$?"

# Phase 2: Wait for /clear to process, then send resume
echo "phase 2: settle delay {clear_settle_delay}s"
sleep {clear_settle_delay}

echo "phase 2: waiting for Claude to be idle after /clear"
{idle_wait_2}

echo "phase 2: re-focusing Terminal window"
FOCUS_RESULT=$(osascript -e {_shell_quote(as_focus)})
echo "phase 2: focus result=$FOCUS_RESULT"
if [ "$FOCUS_RESULT" != "ok" ]; then
    echo "phase 2: ABORTING — could not re-focus terminal"
    rm -f {_shell_quote(lock_path_str)}
    exit 1
fi

echo "phase 2: typing resume prompt"
osascript -e {_shell_quote(as_resume)}
echo "phase 2: resume sent, exit=$?"

# Restore focus
sleep 0.5
rm -f {_shell_quote(lock_path_str)}
echo "=== self-clear complete $(date) ==="
'''
        log.debug("schedule_self_clear: using Terminal.app two-phase path (tty=%r)", claude_tty or "")

    log.debug("schedule_self_clear: shell script:\n%s", shell_cmd)

    # Spawn the orchestrator shell script in the background
    subprocess.Popen(
        ["bash", "-c", shell_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    log.info("schedule_self_clear: background process spawned, will fire in %d seconds", delay_seconds)
    return True


def trigger_self_clear_if_enabled(session_id):
    """Check if auto-clear is enabled and trigger it.

    Auto-clear is controlled by a flag file. The user can enable/disable it:
      touch ~/.cache/context-report/auto-clear-enabled    # enable
      rm ~/.cache/context-report/auto-clear-enabled       # disable
    """
    flag_file = Path.home() / ".cache" / "context-report" / "auto-clear-enabled"
    if flag_file.exists():
        log.info("trigger_self_clear_if_enabled: auto-clear IS enabled, triggering for session %s", session_id)
        return schedule_self_clear(delay_seconds=4)
    log.debug("trigger_self_clear_if_enabled: auto-clear not enabled (no flag file)")
    return False


def graceful_reset():
    """Claude-initiated graceful reset at any context percentage.

    This is the active counterpart to the passive 60% auto-clear.
    Claude calls this when it decides a reset is the smart move — maybe
    at 40% before a huge task, or at 50% at a natural stopping point.

    Pre-flight checks:
    - .remember/now.md must exist and have been updated in the last 2 minutes
      (proves Claude actually saved state, not just called this blindly)
    - Auto-clear must be enabled (same gate as the 60% path)

    On success: schedules the /clear + resume prompt, same as the 60% path.
    """
    log.info("graceful_reset: Claude-initiated reset requested")

    # Check auto-clear is enabled
    flag_file = Path.home() / ".cache" / "context-report" / "auto-clear-enabled"
    if not flag_file.exists():
        msg = (
            "Auto-clear is not enabled. Enable it first:\n"
            "  python3 context_report.py --enable-auto-clear"
        )
        print(msg, file=sys.stderr)
        log.warning("graceful_reset: auto-clear not enabled")
        return False

    # Check .remember/now.md was recently updated (within 2 minutes)
    # Look in the current directory first, then common locations
    remember_candidates = [
        Path.cwd() / ".remember" / "now.md",
        Path.home() / ".remember" / "now.md",
    ]
    # Also check any project directory we might be in
    git_root = _find_git_root()
    if git_root:
        remember_candidates.insert(0, git_root / ".remember" / "now.md")

    now_md = None
    for candidate in remember_candidates:
        if candidate.exists():
            now_md = candidate
            break

    if not now_md:
        msg = (
            "No .remember/now.md found. Save your state first:\n"
            "  1) Write handoff notes to .remember/now.md\n"
            "  2) Run --set-addendum with session context\n"
            "  3) Then run --graceful-reset"
        )
        print(msg, file=sys.stderr)
        log.warning("graceful_reset: no .remember/now.md found")
        return False

    age_seconds = time.time() - now_md.stat().st_mtime
    if age_seconds > 120:
        msg = (
            f".remember/now.md is {int(age_seconds)}s old (max 120s).\n"
            f"Update it with fresh handoff notes before resetting.\n"
            f"This check ensures you actually saved state, not just called --graceful-reset blindly."
        )
        print(msg, file=sys.stderr)
        log.warning("graceful_reset: now.md too stale (%ds old)", int(age_seconds))
        return False

    # All checks pass — schedule the clear+resume
    log.info("graceful_reset: all checks passed, scheduling self-clear")
    success = schedule_self_clear(delay_seconds=4)
    if success:
        print("Graceful reset scheduled. /clear + resume prompt will fire in ~4 seconds.")
        print("Finish your current response — the reset happens after you stop talking.")
    return success


def _find_git_root():
    """Find the git root of the current working directory, if any."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


if __name__ == "__main__":
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if "--hook" in flags:
        transcript_path = find_transcript(sys.argv)
        hook_mode(transcript_path)
    elif "--self-clear" in flags:
        # Manual trigger for testing
        success = schedule_self_clear(delay_seconds=2)
        if success:
            print("Self-clear scheduled (2 second delay)")
        else:
            print("Self-clear not available on this platform")
    elif "--enable-auto-clear" in flags:
        flag = Path.home() / ".cache" / "context-report" / "auto-clear-enabled"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        print(f"Auto-clear ENABLED. Claude will type /clear after saving state at 60%.")
        print(f"To disable: python3 {sys.argv[0]} --disable-auto-clear")
    elif "--disable-auto-clear" in flags:
        flag = Path.home() / ".cache" / "context-report" / "auto-clear-enabled"
        flag.unlink(missing_ok=True)
        print("Auto-clear DISABLED. Claude will ask you to type /clear instead.")
    elif "--set-resume-prompt" in flags:
        # Everything after --set-resume-prompt is the prompt text
        idx = sys.argv.index("--set-resume-prompt")
        prompt_text = " ".join(sys.argv[idx + 1:])
        if not prompt_text.strip():
            print("Usage: python3 context_report.py --set-resume-prompt <your prompt text>")
            sys.exit(1)
        prompt_file = Path.home() / ".cache" / "context-report" / "resume-prompt.txt"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt_text.strip())
        print(f"Resume prompt set to:")
        print(f"  {prompt_text.strip()}")
    elif "--show-resume-prompt" in flags:
        print(f"Current resume prompt:")
        print(f"  {get_resumption_prompt()}")
    elif "--reset-resume-prompt" in flags:
        prompt_file = Path.home() / ".cache" / "context-report" / "resume-prompt.txt"
        prompt_file.unlink(missing_ok=True)
        print(f"Resume prompt reset to default:")
        print(f"  {get_resumption_prompt()}")
    elif "--set-addendum" in flags:
        idx = sys.argv.index("--set-addendum")
        addendum_text = " ".join(sys.argv[idx + 1:])
        if not addendum_text.strip():
            print("Usage: python3 context_report.py --set-addendum <session-specific context>")
            print("Example: python3 context_report.py --set-addendum 'Was debugging auth middleware in src/auth.py, found the token expiry bug on line 142, need to write tests next'")
            sys.exit(1)
        write_session_addendum(addendum_text)
        print(f"Session addendum set (will be consumed on next resume):")
        print(f"  {addendum_text.strip()}")
    elif "--show-addendum" in flags:
        addendum_file = Path.home() / ".cache" / "context-report" / "resume-addendum.txt"
        if addendum_file.exists():
            text = addendum_file.read_text().strip()
            if text:
                print(f"Pending session addendum (will be consumed on next resume):")
                print(f"  {text}")
            else:
                print("No session addendum set.")
        else:
            print("No session addendum set.")
    elif "--clear-addendum" in flags:
        addendum_file = Path.home() / ".cache" / "context-report" / "resume-addendum.txt"
        addendum_file.unlink(missing_ok=True)
        print("Session addendum cleared.")
    elif "--graceful-reset" in flags:
        success = graceful_reset()
        sys.exit(0 if success else 1)
    else:
        transcript_path = find_transcript(sys.argv)
        report(transcript_path)
