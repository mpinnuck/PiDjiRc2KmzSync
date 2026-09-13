from dataclasses import dataclass


@dataclass
class RC2Mission:
    guid: str               # GUID folder name on RC-2
    kmz_name: str           # Existing .kmz filename inside the GUID folder, or "" if empty
    full_folder_path: str
    last_modified: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.kmz_name

    @property
    def display_kmz_name(self) -> str:
        return self.kmz_name if self.kmz_name else "[Empty Slot]"

    @property
    def display_last_modified(self) -> str:
        return self.last_modified if self.last_modified else "[Unknown]"
