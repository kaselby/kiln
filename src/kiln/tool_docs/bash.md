### Bash

Executes a bash command in a persistent shell. Environment variables, working directory, and other state persist between calls.

- This is the primary tool for interacting with the system. File operations, tool invocations, git commands, and most actions flow through Bash.
- The shell session persists across calls — `cd`, `export`, aliases, and variables set in one call are available in the next.
- Use the `description` parameter to briefly note what the command does. This aids readability but is optional.
- Default timeout is 120 seconds. Use the `timeout` parameter (in milliseconds) for long-running commands.
- Use `run_in_background` for commands that may run indefinitely (servers, watchers, long builds). Returns a `job_id` to check status later with `background_job_id`.
- Use `cleanup_background_job_id` to clean up temp files for a finished background job.
- Make parallel Bash calls when the commands are independent of each other.
