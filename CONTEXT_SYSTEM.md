# Context Self-Awareness System — Operator Guide for Claude

You are reading this because a user has handed you this document so you understand how their context management system works. This file tells you everything you need to operate within it.

## What This System Does

You (Claude Code) are running inside a session with a finite context window. This system monitors how full your context window is and automates the save-state-and-reset cycle so you never lose work to context overflow.

A Python script (`context_report.py`) runs as a **Stop hook** after every one of your responses. It reads your transcript JSONL file, calculates how much of the context window you've consumed, and injects messages back to you at escalating thresholds.

## How It Works — The Lifecycle

```
You respond → Stop hook fires → context_report.py reads transcript
  → calculates used% → if threshold crossed, injects message to you
  → you see the message in your next turn as additionalContext
```

### Threshold Alerts

| % Used | Key | What Happens |
|--------|-----|--------------|
| 10% | `context-10` | Informational. "Plenty of room." |
| 20% | `context-20` | Informational. "Healthy range." |
| 30% | `context-30` | Informational. "Still in the sweet spot." |
| 40% | `context-40` | **Early reset available.** If you can see a large task ahead (big codebase exploration, multi-file refactor, lengthy debugging), you *may* choose to reset now. Only do this if the next task is genuinely large. |
| 50% | `context-50` | **Decision point.** Two paths: reset now (Path A) or continue to 60% auto-reset (Path B). Evaluate remaining work and choose. |
| 60% | `context-60` | **Mandatory.** You are told to save state immediately. On the *next* Stop hook after this (i.e., after you've had one turn to save), auto-clear fires. |

Each threshold fires **once per session** — you won't see the same alert twice.

## What You Must Do When Told to Save State

When you receive a 50% (Path A) or 60% alert telling you to save and reset, follow these steps **in order**:

### Step 1: Update handoff notes
Write a summary to `.remember/now.md` in the project directory. Include:
- What was accomplished this session
- What task is next / in progress
- Any blockers or decisions made
- Key file paths that matter

### Step 2: Set a session addendum
Run this command to give the next session targeted context:
```bash
python3 '/Users/db/Code Projects/claude_context/context_report.py' --set-addendum 'Brief description of current focus, critical state, and immediate next step'
```
This text gets injected into the resume prompt that wakes up the next session. It is **consumed after use** (deleted once read), so it won't leak into unrelated future sessions.

### Step 3: Trigger the reset (if not at 60%)
If you're at 40% or 50% and choosing to reset early:
```bash
python3 '/Users/db/Code Projects/claude_context/context_report.py' --graceful-reset
```
This has safety checks:
- `.remember/now.md` must exist and have been updated within the last 2 minutes
- Auto-clear must be enabled

If you're at 60%, you don't need to call `--graceful-reset` — the system handles it automatically on the next Stop hook after you save.

### Step 4: Finish your response
After saving state, complete your current response normally. The background process will:
1. Wait for you to stop generating (transcript goes idle)
2. Focus the terminal window
3. Type `/clear` + Enter
4. Wait for the clear to process
5. Type the resume prompt + Enter

You don't control this — it happens automatically.

## What Happens After /clear

The system types a resume prompt into the terminal. The default prompt is:

> "Resuming after auto-clear. Read .remember/now.md for session handoff notes. Check memory files in the project memory directory for broader context. Then continue with the next task from where the previous session left off. Summarize what you found in the handoff notes before proceeding."

If a session addendum was set, it gets appended: `"Additionally: <your addendum text>"`

**When you wake up as the new session**, your job is:
1. Read `.remember/now.md`
2. Check the project memory directory (`~/.claude/projects/*/memory/MEMORY.md`)
3. Summarize what the previous session was doing
4. Continue the work

## Files and Locations

| Path | Purpose |
|------|---------|
| `/Users/db/Code Projects/claude_context/context_report.py` | The main script |
| `~/.claude/settings.json` | Stop hook configuration |
| `~/.cache/context-report/<session-id>.json` | Per-session threshold state |
| `~/.cache/context-report/auto-clear-enabled` | Flag file — presence means auto-clear is on |
| `~/.cache/context-report/tty-cache` | Cached terminal TTY path |
| `~/.cache/context-report/resume-prompt.txt` | Custom base resume prompt (optional) |
| `~/.cache/context-report/resume-addendum.txt` | One-shot session addendum (consumed on read) |
| `~/.cache/context-report/clear-pending` | Lock file to prevent duplicate clears |
| `~/.cache/context-report/debug.log` | Debug log for troubleshooting |
| `~/.cache/context-report/self-clear.log` | Log from the background clear process |
| `.remember/now.md` | Session handoff notes (in the project directory) |

## CLI Commands You Can Run

```bash
# Check current context usage (human-readable)
python3 '/Users/db/Code Projects/claude_context/context_report.py'

# Enable/disable auto-clear
python3 '/Users/db/Code Projects/claude_context/context_report.py' --enable-auto-clear
python3 '/Users/db/Code Projects/claude_context/context_report.py' --disable-auto-clear

# Set session addendum for next resume
python3 '/Users/db/Code Projects/claude_context/context_report.py' --set-addendum 'your context here'

# Peek at pending addendum
python3 '/Users/db/Code Projects/claude_context/context_report.py' --show-addendum

# Clear addendum without consuming it
python3 '/Users/db/Code Projects/claude_context/context_report.py' --clear-addendum

# View current resume prompt
python3 '/Users/db/Code Projects/claude_context/context_report.py' --show-resume-prompt

# Set custom base resume prompt
python3 '/Users/db/Code Projects/claude_context/context_report.py' --set-resume-prompt 'your custom prompt'

# Reset to default resume prompt
python3 '/Users/db/Code Projects/claude_context/context_report.py' --reset-resume-prompt

# Claude-initiated reset at any % (requires fresh now.md + auto-clear enabled)
python3 '/Users/db/Code Projects/claude_context/context_report.py' --graceful-reset

# Manual self-clear test (2s delay)
python3 '/Users/db/Code Projects/claude_context/context_report.py' --self-clear
```

## The Stop Hook Configuration

In `~/.claude/settings.json`, the hook is configured as:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 '/Users/db/Code Projects/claude_context/context_report.py' --hook 2>/dev/null || true",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

The `--hook` flag makes the script output JSON that Claude Code's hook system understands. The `2>/dev/null || true` ensures hook failures never block your responses.

## How Context Window Size Is Detected

The script reads the model name from the transcript and maps it:
- Opus 4.6 or Sonnet 4.6 → 1,048,576 tokens (1M)
- All other models → 200,000 tokens

## How the Auto-Clear Mechanism Works (macOS only)

The self-clear uses AppleScript to type into the terminal. It supports:
- **Terminal.app** — finds the correct window by TTY, uses keystroke simulation
- **iTerm2** — uses `write text` directly to the correct session (more reliable)
- **Warp** — keystroke simulation with app focus

The TTY is cached during every hook invocation (when the process tree is reliable) so the background clear process can find the right terminal later.

The two-phase idle detection watches the transcript JSONL file's modification time. When it stops changing for 3+ seconds, Claude is idle at the prompt. This prevents typing `/clear` while you're still generating output.

## Behavioral Guidelines

- **Don't ignore threshold messages.** They appear as `additionalContext` in your turn. Act on them.
- **At 40%, only reset if the next task is genuinely large.** Don't reset just because you can.
- **At 50%, make a real decision.** Evaluate: is remaining work small enough to finish before 60%? If yes, continue. If no, reset.
- **At 60%, save state is mandatory.** Don't try to squeeze in more work. Save first, then finish your response.
- **Write good handoff notes.** The next session is you with amnesia. Be specific: file paths, line numbers, decisions made, what's next.
- **Keep addendums short and actionable.** The addendum is injected as part of the prompt — don't write a novel. One or two sentences about current focus and next step.
- **Never call `--graceful-reset` without saving state first.** The safety check will reject it if `now.md` is stale.
