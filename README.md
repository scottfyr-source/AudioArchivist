# CDRipper

A simple Windows CD ripper with a GUI front end and MusicBrainz metadata lookup.

## What this project includes

- `cd_ripper.py` — main GUI application
- `requirements.txt` — Python dependencies

## Features

- Detect available CD-ROM drives on Windows
- Read audio CD track positions
- Calculate a MusicBrainz disc ID and query metadata
- Display album and track metadata in the GUI
- Rip audio tracks to WAV files
- Tag WAV files with basic track metadata using Mutagen

## Dependencies

Install the dependencies using your virtual environment:

```powershell
python -m pip install -r requirements.txt
```

## Running the app

```powershell
python cd_ripper.py
```

## Notes

- This implementation is Windows-only.
- The GUI uses `PySimpleGUI`.
- MusicBrainz metadata lookup requires an internet connection.
- Raw audio reading from the CD drive may require administrative permissions.
- If you want to use `FreeSimpleGUI` instead, the code can be adapted to that API.

## Troubleshooting

- If the app cannot find a CD drive, make sure an audio CD is inserted and the drive is recognized by Windows.
- If metadata lookup fails, verify your internet connection and try a manual artist/album search.
- If raw ripping fails, the drive may not support direct raw-sector reads or the app may need elevated privileges.
