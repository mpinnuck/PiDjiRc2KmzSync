from dataclasses import dataclass


@dataclass
class KMZFile:
    filename: str       # e.g. "survey_grid.kmz" (relative to configured PC root)
    full_path: str      # Absolute path to the file
    last_written: str = ""  # Local filesystem mtime in "YYYY-MM-DD HH:MM:SS"
