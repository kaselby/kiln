### Read

Reads a file from the local filesystem. The `file_path` parameter must be an absolute path.

- By default, it reads from the beginning of the file up to a character budget (~45K chars). For large files, output is truncated at the last complete line with a message showing how many lines remain and what offset to use to continue.
- Use `offset` and `limit` to read specific sections of large files. This is the recommended approach for files over a few hundred lines — read the section you need rather than the entire file.
- Lines longer than 2000 characters are truncated (marked with `[truncated]`).
- Results are returned in cat -n format, with line numbers starting at 1.
- Can only read files, not directories. Use `ls` via Bash for directories.
- Read multiple potentially useful files in parallel when possible.
- Can read images (PNG, JPG, GIF, WebP). Image content is presented visually.
- Can read Jupyter notebooks (.ipynb) and returns all cells with their outputs.
- Can read PDF files (.pdf). PDF content is injected as a native document for full reading. Use the `pages` parameter to read specific pages (e.g., '1-3', '5', '10-') to save context on large PDFs.
