### Read

The `file_path` parameter must be an absolute path.

- By default reads up to 2000 lines from the beginning of the file. You can specify an offset and limit for long files.
- Lines longer than 2000 characters are truncated.
- Results are returned in cat -n format, with line numbers starting at 1.
- Can only read files, not directories. Use `ls` via Bash for directories.
- Read multiple potentially useful files in parallel when possible.
