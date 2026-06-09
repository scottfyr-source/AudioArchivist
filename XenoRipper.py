import ctypes
import os
import platform
import re
import threading
import traceback
import wave
import json
import socket
import webbrowser
import subprocess
import io
import winsound
from urllib.request import urlopen
from ctypes import WinDLL, create_unicode_buffer, windll
from ctypes import c_ulong, c_ushort, c_ubyte, Structure, sizeof
from ctypes import wintypes

try:
    import PySimpleGUI as sg
except ImportError:
    sg = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

try:
    import musicbrainzngs
except ImportError:
    musicbrainzngs = None

try:
    from mutagen.wave import WAVE
except Exception as e:
    WAVE = None
    # This helps diagnose why mutagen might not be loading even if installed
    print(f"Warning: Could not import mutagen.wave (required for WAV tagging): {e}")


MUSICBRAINZ_AGENT_NAME = "AudioArchivist"
MUSICBRAINZ_AGENT_VERSION = "0.1"
MUSICBRAINZ_AGENT_CONTACT = "you@example.com"

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=4)
    except Exception as e:
        print(f"Failed to save settings: {e}")

IOCTL_CDROM_RAW_READ = 0x0002403E
SECTOR_SIZE = 2352
FRAMES_PER_SECOND = 75
SAMPLES_PER_SECOND = 44100


class RawReadInfo(Structure):
    _fields_ = [
        ("DiskOffset", ctypes.c_longlong),
        ("SectorCount", ctypes.c_ulong),
        ("TrackMode", ctypes.c_int),
    ]


def ensure_dependencies():
    if sg is None:
        raise RuntimeError("Missing dependency: PySimpleGUI is required. Install `pip install PySimpleGUI`.")
    if musicbrainzngs is None:
        raise RuntimeError("Missing dependency: musicbrainzngs is required. Install `pip install musicbrainzngs`.")
    if WAVE is None:
        raise RuntimeError("Missing dependency: mutagen is required. Install `pip install mutagen`.")


def list_cdrom_drives():
    if platform.system() != "Windows":
        return []

    kernel32 = windll.kernel32
    drive_mask = kernel32.GetLogicalDrives()
    cdrom_drives = []
    for index in range(26):
        if drive_mask & (1 << index):
            drive_letter = chr(ord("A") + index)
            drive_type = kernel32.GetDriveTypeW(f"{drive_letter}:\\")
            if drive_type == 5:  # DRIVE_CDROM
                cdrom_drives.append(drive_letter)
    return cdrom_drives


def mci_error_string(error_code):
    buffer = create_unicode_buffer(255)
    if windll.winmm.mciGetErrorStringW(error_code, buffer, sizeof(buffer)):
        return buffer.value.strip()
    return "Unknown MCI error"


def mci_send_string(command):
    buffer = create_unicode_buffer(255)
    error_code = windll.winmm.mciSendStringW(command, buffer, 254, 0)
    if error_code != 0:
        error_text = mci_error_string(error_code)
        raise RuntimeError(f"MCI command failed ({error_code}): {command} => {error_text}")
    return buffer.value.strip()


def parse_tmsf(value):
    if not value:
        return 0
    parts = [int(part) for part in value.strip().split(":") if part != ""]
    if len(parts) == 4:
        # Some MCI responses include track number or an extra zero field before the MSF triple.
        parts = parts[1:]
    if len(parts) == 3:
        minutes, seconds, frames = parts
    elif len(parts) == 2:
        minutes, seconds = parts
        frames = 0
    else:
        raise ValueError(f"Invalid time format: {value}")
    return (minutes * 60 + seconds) * FRAMES_PER_SECOND + frames


def format_tmsf(frames):
    seconds_total = frames // FRAMES_PER_SECOND
    frames_remainder = frames % FRAMES_PER_SECOND
    minutes = seconds_total // 60
    seconds = seconds_total % 60
    return f"{minutes:02d}:{seconds:02d}:{frames_remainder:02d}"


def calculate_disc_id(track_offsets, leadout_offset):
    def csum(value):
        total = 0
        while value > 0:
            total += value % 10
            value //= 10
        return total

    checksum = sum(csum(offset // FRAMES_PER_SECOND) for offset in track_offsets)
    track_count = len(track_offsets)
    leadout_seconds = leadout_offset // FRAMES_PER_SECOND
    disc_id = ((checksum % 255) << 24) | (leadout_seconds << 8) | track_count
    return f"{disc_id:08x}"


def build_toc_string(track_offsets, leadout_offset):
    # MusicBrainz/discid expects TOC in format: "<first_track> <last_track> <leadout_sector> <track_1_sector> ..."
    # Offsets are already in frames/sectors (1 sector = 75 frames means 1 second = 75 sectors, but the values are already in sectors).
    if not track_offsets:
        return None
    first_track = 1
    last_track = len(track_offsets)
    values = [str(first_track), str(last_track), str(leadout_offset)]
    for offset in track_offsets:
        values.append(str(offset))
    return " ".join(values)


def normalize_release_response(result):
    if isinstance(result, dict):
        if "release" in result:
            return result["release"]
        if "disc" in result:
            disc = result["disc"]
            if disc.get("release-list"):
                return disc["release-list"][0]
        if "release-list" in result and result["release-list"]:
            return result["release-list"][0]
        if "cdstub" in result:
            return result["cdstub"]
    return result


def parse_musicbrainz_id(value):
    if not value:
        return None
    value = value.strip()
    match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return value
    return None


def get_clipboard_text(window):
    try:
        return window.TKroot.clipboard_get().strip()
    except Exception:
        return None


def detect_clipboard_musicbrainz_release(window):
    clipboard_text = get_clipboard_text(window)
    if not clipboard_text:
        return None
    release_id = parse_musicbrainz_id(clipboard_text)
    if not release_id:
        return None
    if not clipboard_text.lower().startswith("http"):
        clipboard_text = f"https://musicbrainz.org/release/{release_id}"
    return clipboard_text, release_id


def get_musicbrainz_release_by_id(release_id):
    musicbrainzngs.set_useragent(MUSICBRAINZ_AGENT_NAME, MUSICBRAINZ_AGENT_VERSION, MUSICBRAINZ_AGENT_CONTACT)
    try:
        result = musicbrainzngs.get_release_by_id(release_id, includes=["artists", "recordings", "labels", "release-groups", "tags"])
        return normalize_release_response(result)
    except Exception as exc:
        raise RuntimeError(f"MusicBrainz release lookup failed: {exc}")


def open_cd_drive(drive_letter, alias="cd"):
    commands = [
        f"open {drive_letter}: type cdaudio alias {alias} shareable",
        f"open {drive_letter}: type cdaudio alias {alias}",
    ]
    last_exc = None
    for command in commands:
        try:
            return mci_send_string(command)
        except RuntimeError as exc:
            last_exc = exc
    raise last_exc


def close_cd_drive(alias="cd"):
    try:
        return mci_send_string(f"close {alias}")
    except RuntimeError:
        return None


def eject_cd_drive(drive_letter):
    """
    Eject the CD from the drive using MCI commands.
    """
    alias = f"cd_eject_{drive_letter}"
    try:
        open_cd_drive(drive_letter, alias=alias)
        mci_send_string(f"set {alias} door open")
        close_cd_drive(alias=alias)
        return True
    except Exception as exc:
        return False


def get_cd_toc(drive_letter, alias="cd"):
    open_cd_drive(drive_letter, alias=alias)
    try:
        mci_send_string(f"set {alias} time format msf")
        mode = mci_send_string(f"status {alias} mode")
        if mode.lower() not in ("ready", "stopped", "playing"):
            raise RuntimeError(f"Drive {drive_letter} is not ready for audio CD access (mode={mode}).")

        start_positions = []
        track_lengths = []
        try:
            track_count = int(mci_send_string(f"status {alias} numberOfTracks"))
            if track_count <= 0:
                raise RuntimeError(f"No audio tracks found on drive {drive_letter}.")
            for track_number in range(1, track_count + 1):
                start_positions.append(parse_tmsf(mci_send_string(f"status {alias} position track {track_number}")))
                track_lengths.append(parse_tmsf(mci_send_string(f"status {alias} length track {track_number}")))
        except RuntimeError as exc:
            message = str(exc).lower()
            if "numberoftracks" in message or "specified parameter is invalid" in message or "290" in message:
                # Fallback for drives rejecting numberOfTracks: enumerate track numbers manually.
                for track_number in range(1, 100):
                    try:
                        start_positions.append(parse_tmsf(mci_send_string(f"status {alias} position track {track_number}")))
                        track_lengths.append(parse_tmsf(mci_send_string(f"status {alias} length track {track_number}")))
                    except RuntimeError:
                        break
                if not start_positions:
                    raise RuntimeError(f"No audio tracks found on drive {drive_letter}, and numberOfTracks is unsupported.")
            else:
                raise
        
        total_length = start_positions[-1] + track_lengths[-1]
        return start_positions, track_lengths, total_length
    finally:
        close_cd_drive(alias=alias)


CDDB_SERVER = "gnudb.gnudb.org"
CDDB_PORT = 8880
CDDB_CLIENT_NAME = "AudioArchivist"
CDDB_CLIENT_VERSION = "1.0"
CDDB_USER = "anonymous"


def get_musicbrainz_release_by_discid(disc_id, track_positions=None, leadout_offset=None):
    musicbrainzngs.set_useragent(MUSICBRAINZ_AGENT_NAME, MUSICBRAINZ_AGENT_VERSION, MUSICBRAINZ_AGENT_CONTACT)
    attempts = []
    # First try: disc ID lookup without TOC
    if "-" in disc_id: # Only try exact match if it looks like a MusicBrainz Disc ID
        attempts.append(("disc_id_only", lambda: musicbrainzngs.get_releases_by_discid(
            disc_id,
            includes=["artists", "recordings", "labels", "release-groups"],
            cdstubs=False,
        )))
    # Second try: with TOC if available
    if track_positions is not None and leadout_offset is not None:
        toc = build_toc_string(track_positions, leadout_offset)
        if toc:
            attempts.append(("disc_id_with_toc", lambda toc_val=toc: musicbrainzngs.get_releases_by_discid(
                "-",
                includes=["artists", "recordings", "labels", "release-groups"],
                toc=toc_val,
                cdstubs=False,
            )))

    last_exception = None
    for name, call in attempts:
        try:
            result = call()
            release = normalize_release_response(result)
            if release:
                return release
        except Exception as exc:
            last_exception = exc

    if last_exception:
        raise RuntimeError(f"MusicBrainz disc lookup failed: {last_exception}")
    raise RuntimeError("No MusicBrainz release found for disc ID.")


def query_cddb_release(disc_id, track_positions, leadout_offset):
    if not track_positions:
        raise ValueError("No track positions provided for CDDB lookup.")

    offsets = [position // FRAMES_PER_SECOND for position in track_positions]
    num_tracks = len(offsets)
    leadout_seconds = leadout_offset // FRAMES_PER_SECOND

    with socket.create_connection((CDDB_SERVER, CDDB_PORT), timeout=10) as sock:
        file = sock.makefile("rw", encoding="utf-8", newline="\r\n")
        banner = file.readline().strip()
        if not banner.startswith("2"):
            raise RuntimeError(f"CDDB server banner error: {banner}")

        hostname = platform.node() or "localhost"
        file.write(f"cddb hello {CDDB_USER} {hostname} {CDDB_CLIENT_NAME} {CDDB_CLIENT_VERSION}\r\n")
        file.flush()
        response = file.readline().strip()
        if not response.startswith("2"):
            raise RuntimeError(f"CDDB hello failed: {response}")

        file.write("proto 6\r\n")
        file.flush()
        response = file.readline().strip()
        if not response.startswith("2"):
            raise RuntimeError(f"CDDB proto failed: {response}")

        query = f"cddb query {disc_id} {num_tracks} {' '.join(str(offset) for offset in offsets)} {leadout_seconds}"
        file.write(query + "\r\n")
        file.flush()
        response = file.readline().strip()
        if response.startswith("202"):
            raise RuntimeError("CDDB query did not find a matching disc.")

        category = None
        title_line = ""
        if response.startswith("200") or response.startswith("210"):
            parts = response.split(" ", 2)
            if len(parts) >= 3:
                category = parts[1]
                title_line = parts[2]
        elif response.startswith("211"):
            matches = []
            while True:
                line = file.readline().strip()
                if line == ".":
                    break
                if line:
                    matches.append(line)
            if not matches:
                raise RuntimeError("CDDB query returned multiple matches but no candidates.")
            parts = matches[0].split(" ", 2)
            if len(parts) >= 3:
                category = parts[1]
                title_line = parts[2]
        else:
            raise RuntimeError(f"Unexpected CDDB query response: {response}")

        if not category:
            raise RuntimeError("CDDB query response did not include a category.")

        file.write(f"cddb read {category} {disc_id}\r\n")
        file.flush()
        response = file.readline().strip()
        if not response.startswith("2"):
            raise RuntimeError(f"CDDB read failed: {response}")

        disc_title = ""
        track_titles = []
        while True:
            line = file.readline()
            if not line:
                raise RuntimeError("CDDB read response ended unexpectedly.")
            value = line.strip()
            if value == ".":
                break
            if value.startswith("DTITLE="):
                disc_title = value.split("=", 1)[1]
            elif value.startswith("TTITLE"):
                _, title_value = value.split("=", 1)
                track_titles.append(title_value)

        artist = ""
        album = title_line or disc_title
        if " / " in album:
            artist, album = [part.strip() for part in album.split(" / ", 1)]
        elif " / " in disc_title:
            artist, album = [part.strip() for part in disc_title.split(" / ", 1)]
        else:
            album = album.strip()

        lengths = []
        if len(track_positions) > 1:
            for index in range(len(track_positions)):
                start = track_positions[index]
                end = track_positions[index + 1] if index + 1 < len(track_positions) else leadout_offset
                lengths.append(int((end - start) / FRAMES_PER_SECOND * 1000))
        else:
            lengths = [0] * len(track_titles)

        tracks = []
        for index, title in enumerate(track_titles):
            tracks.append({
                "number": index + 1,
                "title": title or f"Track {index + 1}",
                "length_ms": lengths[index] if index < len(lengths) else 0,
            })

        return {
            "title": album,
            "artist": artist,
            "date": "",
            "label": "",
            "genre": "",
            "url": f"cddb://{CDDB_SERVER}/{category}/{disc_id}",
            "cover_url": None,
            "release_id": disc_id,
            "tracks": tracks,
            "manual_cover_path": None,
        }


def search_musicbrainz_release(artist, title):
    musicbrainzngs.set_useragent(MUSICBRAINZ_AGENT_NAME, MUSICBRAINZ_AGENT_VERSION, MUSICBRAINZ_AGENT_CONTACT)
    try:
        result = musicbrainzngs.search_releases(artist=artist, release=title, limit=5)
        if not result.get("release-list"):
            raise RuntimeError("No MusicBrainz releases found for this search.")
        return result["release-list"][0]
    except Exception as exc:
        raise RuntimeError(f"MusicBrainz search failed: {exc}")

def fetch_cover_art_url(release_id):
    try:
        images = musicbrainzngs.get_image_list(release_id)
        for image in images.get("images", []):
            if image.get("front"):
                return image.get("image")
    except Exception:
        pass
    return None


def parse_release_metadata(release):
    if not isinstance(release, dict):
        return {"title": "", "artist": "", "date": "", "tracks": [], "label": "", "genre": "", "url": "", "cover_url": "", "release_id": "", "manual_cover_path": None}

    artist_credit = release.get("artist-credit", [])
    artists = []
    for item in artist_credit:
        if isinstance(item, dict):
            name = item.get("name") or item.get("artist", {}).get("name")
            if name:
                artists.append(name)
    if not artists and release.get("artist"):
        artists = [release.get("artist")]

    title = release.get("title") or release.get("name") or ""
    date = release.get("date", "")

    label = ""
    if "label-info-list" in release:
        labels = [l["label"]["name"] for l in release["label-info-list"] if "label" in l and "name" in l["label"]]
        label = ", ".join(labels)

    genre = ""
    tags = release.get("tag-list", [])
    if not tags and "release-group" in release:
        tags = release["release-group"].get("tag-list", [])
    if tags:
        tags = sorted(tags, key=lambda x: int(x.get('count', 0)), reverse=True)
        genre = ", ".join([t["name"] for t in tags[:3]])

    release_id = release.get("id", "")
    url = f"https://musicbrainz.org/release/{release_id}" if release_id else ""
    cover_url = fetch_cover_art_url(release_id) if release_id else None

    media = release.get("medium-list", [])
    tracks = []
    if media:
        for medium in media:
            for track in medium.get("track-list", []):
                track_title = track.get("recording", {}).get("title", "Track")
                track_number = track.get("number")
                length_ms = int(track.get("length", 0))
                tracks.append({
                    "number": track_number,
                    "title": track_title,
                    "length_ms": length_ms,
                })
    elif release.get("tracks"):
        for track in release.get("tracks", []):
            tracks.append({
                "number": track.get("number"),
                "title": track.get("title", "Track"),
                "length_ms": int(track.get("length", 0)),
            })

    return {
        "title": title,
        "artist": ", ".join(artists) if artists else "",
        "date": date,
        "label": label,
        "genre": genre,
        "url": url,
        "cover_url": cover_url,
        "release_id": release_id,
        "tracks": tracks,
        "manual_cover_path": None,
    }


def format_track_display(track_info, start_position):
    duration_str = ""
    if track_info and track_info.get("length_ms"):
        duration_str = f" ({track_info['length_ms'] // 1000 // 60}:{(track_info['length_ms'] // 1000) % 60:02d})"
    start_str = format_tmsf(start_position)
    return f"{track_info['number']:>2}. {track_info['title']}{duration_str}  [{start_str}]"





def create_drive_handle(drive_letter):
    path = fr"\\.\{drive_letter}:"
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    OPEN_EXISTING = 3
    handle = windll.kernel32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if handle == -1:
        raise OSError(f"Unable to open drive {drive_letter}: check permissions and that the drive exists.")
    return handle


def raw_read_sectors(handle, start_sector, sector_count):
    info = RawReadInfo(start_sector * 2048, sector_count, 2) # TrackMode 2 is CDDA
    buffer_size = sector_count * SECTOR_SIZE
    buffer = ctypes.create_string_buffer(buffer_size)
    bytes_returned = wintypes.DWORD(0)
    success = windll.kernel32.DeviceIoControl(handle, IOCTL_CDROM_RAW_READ, ctypes.byref(info), sizeof(info), buffer, buffer_size, ctypes.byref(bytes_returned), None)
    if not success:
        raise OSError("DeviceIoControl failed while reading raw sectors.")
    return buffer.raw[: bytes_returned.value]


def rip_audio_track(drive_letter, start_frame, end_frame, output_path, progress_callback=None):
    total_sectors = end_frame - start_frame
    if total_sectors <= 0:
        raise ValueError("Invalid track range.")
    handle = create_drive_handle(drive_letter)
    try:
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLES_PER_SECOND)
            sectors_per_batch = 32
            sectors_read = 0
            while sectors_read < total_sectors:
                count = min(sectors_per_batch, total_sectors - sectors_read)
                try:
                    chunk = raw_read_sectors(handle, start_frame + sectors_read, count)
                except OSError:
                    # If a batch read fails (common near the lead-out), try sector-by-sector
                    if count > 1:
                        batch_chunks = []
                        for i in range(count):
                            try:
                                s_chunk = raw_read_sectors(handle, start_frame + sectors_read + i, 1)
                                batch_chunks.append(s_chunk)
                            except OSError:
                                break # Hit the physical end of readable media
                        if not batch_chunks:
                            break
                        chunk = b"".join(batch_chunks)
                    else:
                        # Failed reading a single sector at the very end
                        break

                wf.writeframes(chunk)
                actual_read_count = len(chunk) // SECTOR_SIZE
                sectors_read += actual_read_count
                if progress_callback:
                    progress_callback(sectors_read, total_sectors)
                if actual_read_count < count:
                    break
    finally:
        windll.kernel32.CloseHandle(handle)


def append_log(window, message):
    existing = window["-LOG-"].get()
    updated = existing + ("\n" if existing else "") + message
    window["-LOG-"].update(updated)


def update_track_display(window, metadata, track_positions):
    values = []
    for index, start in enumerate(track_positions):
        track_info = None
        if metadata and index < len(metadata.get("tracks", [])):
            track_info = metadata["tracks"][index]
        else:
            track_info = {"number": index + 1, "title": f"Track {index + 1}", "length_ms": 0}
        values.append(format_track_display(track_info, start))
    window["-TRACKS-"].update(values)


def get_formatted_output_path(root, pattern, artist, album):
    def clean_segment(s):
        invalid = '<>:"|?*'  # Strip characters illegal for folders, allow / \ for pattern
        return "".join(ch for ch in s if ch not in invalid).strip().rstrip('.') or "Unknown"

    artist_clean = clean_segment(artist)
    album_clean = clean_segment(album)
    subfolder = pattern.replace("{Artist}", artist_clean).replace("{Album}", album_clean)
    return os.path.normpath(os.path.join(root, subfolder))


def safe_filename(name, ext=".wav"):
    invalid = '<>:"/\\|?*'
    base = "".join(ch for ch in name if ch not in invalid).strip().rstrip('.') or "track"
    return f"{base}{ext}"


def show_lyrics_window(track_title, lyrics):
    """
    Display lyrics in a movable pop-up window.
    """
    if not lyrics:
        sg.Popup("No lyrics available", title=f"Lyrics for {track_title}")
        return
    
    layout = [
        [sg.Text(f"Lyrics: {track_title}", font=("Any", 12, "bold"))],
        [sg.Multiline(lyrics, size=(60, 25), disabled=True, autoscroll=False, key="-LYRICS-TEXT-")],
        [sg.Button("Close")]
    ]
    
    window = sg.Window(f"Lyrics - {track_title}", layout, finalize=True, resizable=True)
    while True:
        event, values = window.read()
        if event in (sg.WIN_CLOSED, "Close"):
            break
    window.close()


# pyrefly: ignore [invalid-syntax]
def play_cd_track_async(drive_letter, start_frame, end_frame, track_title, window, state=None):
    """
    Rip a track to a temporary WAV and play it in a background thread.
    Updates the window log with playback status.
    """
    try:
        temp_wav = os.path.join(os.environ.get('TEMP', 'C:\\Temp'), f"AudioArchivist_play_{int(os.times()[4]*1000)}.wav")
        append_log(window, f"Playing: {track_title}...")
        rip_audio_track(drive_letter, start_frame, end_frame, temp_wav)
        
        # Fetch and store lyrics if state is provided
        if state is not None:
            lyrics = fetch_lyrics("", track_title)  # We'll get artist from metadata
            state["current_lyrics"] = lyrics
        
        # Play the WAV file
        winsound.PlaySound(temp_wav, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        append_log(window, f"Playback finished: {track_title}")
        
        # Clean up temp file
        try:
            os.remove(temp_wav)
        except:
            pass
    except Exception as exc:
        append_log(window, f"Playback error: {exc}")

def search_by_text(window, artist, album):
    
    try:
        release = search_musicbrainz_release(artist, album)
        if release and "id" in release:
            # Fetch full details including tags/genres
            release = get_musicbrainz_release_by_id(release["id"])
        metadata = parse_release_metadata(release)
        return metadata
    except Exception as exc:
        append_log(window, str(exc))
        return None


def scan_drive(window, drive_letter, state):
    append_log(window, f"Scanning drive {drive_letter}...")
    try:
        positions, lengths, total_length = get_cd_toc(drive_letter)
        state["track_positions"] = positions
        state["track_lengths"] = lengths
        state["leadout"] = total_length
        state["disc_id"] = calculate_disc_id(positions, total_length)
        append_log(window, f"Disc ID: {state['disc_id']}")
        window["-DISC-"].update(state["disc_id"])
        return True
    except Exception as exc:
        append_log(window, f"CD scan failed: {exc}")
        return False


def fill_metadata_fields(window, metadata):
    window["-ALBUM-"].update(value=metadata.get("title", ""))
    window["-ARTIST-"].update(value=metadata.get("artist", ""))
    window["-YEAR-"].update(value=metadata.get("date", ""))
    window["-GENRE-"].update(value=metadata.get("genre", ""))
    window["-LABEL-"].update(value=metadata.get("label", ""))
    window["-MB-URL-DISPLAY-"].update(value=metadata.get("url", ""))
    window["-MB-URL-"].update(value=metadata.get("url", ""))
    if metadata.get("manual_cover_path"):
        update_cover_display(window, metadata.get("manual_cover_path"), is_path=True)
    else:
        update_cover_display(window, metadata.get("cover_url"))

def update_ui_with_metadata(window, metadata, state, settings):
    state["metadata"] = metadata
    fill_metadata_fields(window, metadata)
    update_track_display(window, metadata, state.get("track_positions", []))
    
    root = settings.get("root_destination", settings.get("output_folder", os.path.join(os.path.expanduser("~"), "Music")))
    pattern = settings.get("subfolder_pattern", "{Artist}\\{Album}")
    final_path = get_formatted_output_path(root, pattern, metadata.get("artist", ""), metadata.get("title", ""))
    window["-OUT-"].update(final_path)

def update_cover_display(window, source, is_path=False):
    if not source:
        window["-COVER-"].update(data=None)
        return
    try:
        if is_path:
            with open(source, "rb") as f:
                image_data = f.read()
        else:
            with urlopen(source) as response:
                image_data = response.read()

        if Image:
            img = Image.open(io.BytesIO(image_data))
            img.thumbnail((200, 200))
            bio = io.BytesIO()
            img.save(bio, format="PNG")
            window["-COVER-"].update(data=bio.getvalue())
        else:
            # Fallback to raw data if Pillow is missing
            window["-COVER-"].update(data=image_data)
    except Exception as e:
        print(f"Failed to load cover art: {e}")
        window["-COVER-"].update(data=None)

def create_file_metadata(path, metadata, track_number, track_title, audio_format, lyrics=""):
    if WAVE is None or audio_format.upper() != "WAV":
        return
    try:
        audio = WAVE(path)
        # Initialize tags if the WAV file doesn't have a RIFF INFO block yet
        if audio.tags is None:
            audio.add_tags()
        
        audio.tags["INAM"] = str(track_title)
        audio.tags["IART"] = str(metadata.get("artist", ""))
        audio.tags["IPRD"] = str(metadata.get("title", ""))
        audio.tags["ICRD"] = str(metadata.get("date", ""))
        audio.tags["IGNR"] = str(metadata.get("genre", ""))
        audio.tags["ITRK"] = str(track_number)
        audio.tags["IPUB"] = str(metadata.get("label", ""))
        audio.tags["ICMT"] = str(metadata.get("url", ""))
        if lyrics:
            audio.tags["ILYR"] = str(lyrics)
        audio.save()
    except Exception:
        pass


def fetch_lyrics(artist, track_title):
    """
    Fetch lyrics from ChartLyrics API.
    Returns lyrics string or empty string if not found.
    """
    if not artist or not track_title:
        return ""
    
    try:
        # Sanitize inputs
        artist_clean = re.sub(r'[^\w\s]', '', artist).strip()
        track_clean = re.sub(r'[^\w\s]', '', track_title).strip()
        
        if not artist_clean or not track_clean:
            return ""
        
        # Try ChartLyrics API
        url = f"http://api.chartlyrics.com/apiv1.asmx/SearchLyricsDirect?artist={artist_clean}&song={track_clean}"
        with urlopen(url, timeout=5) as response:
            content = response.read().decode('utf-8', errors='ignore')
            
            # Extract lyrics from XML response
            match = re.search(r'<Lyric>(.*?)</Lyric>', content, re.DOTALL)
            if match:
                lyrics = match.group(1).strip()
                if lyrics and lyrics.lower() not in ('not found', ''):
                    return lyrics
    except Exception:
        pass
    
    return ""

def convert_to_compressed(input_wav, output_file, metadata, track_info, audio_format, cover_path=None):
    ffmpeg_exe = r"C:\Tools\ffmpeg\bin\ffmpeg.exe"
    if not os.path.exists(ffmpeg_exe):
        return False, f"FFmpeg not found at {ffmpeg_exe}"

    cmd = [ffmpeg_exe, "-y", "-i", input_wav]
    has_cover = cover_path and os.path.exists(cover_path)
    if has_cover:
        cmd += ["-i", cover_path]

    # Output options: mapping, metadata, and codec settings
    cmd += ["-map", "0:0"]
    if has_cover:
        cmd += ["-map", "1:0", "-c:v", "copy", "-disposition:v", "attached_pic"]

    cmd += [
        "-metadata", f"title={track_info['title']}",
        "-metadata", f"artist={metadata.get('artist', '')}",
        "-metadata", f"album={metadata.get('title', '')}",
        "-metadata", f"date={metadata.get('date', '')}",
        "-metadata", f"genre={metadata.get('genre', '')}",
        "-metadata", f"track={track_info['number']}",
        "-metadata", f"album_artist={metadata.get('artist', '')}",
        "-metadata", f"publisher={metadata.get('label', '')}",
        "-metadata", f"comment={metadata.get('url', '')}",
    ]

    if audio_format.upper() == "MP3":
        # Use libmp3lame with high quality VBR (approx 190-250 kbps)
        cmd += ["-codec:a", "libmp3lame", "-qscale:a", "2", "-id3v2_version", "3"]
    elif audio_format.upper() == "FLAC":
        cmd += ["-codec:a", "flac"]

    cmd.append(output_file)

    try:
        # Hide the console window on Windows
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        result = subprocess.run(cmd, check=True, startupinfo=startupinfo, capture_output=True, text=True)
        return True, None
    except subprocess.CalledProcessError as e:
        error_msg = f"FFmpeg failed (exit {e.returncode}): {e.stderr or e.stdout}"
        return False, error_msg
    except Exception as e:
        return False, str(e)


def rip_thread(window, state, output_folder, selected_indices, audio_format, fetch_lyrics_enabled=False):
    try:
        drive_letter = state.get("selected_drive")
        if not drive_letter:
            raise RuntimeError("No drive selected.")
        track_positions = state.get("track_positions", [])
        if not track_positions:
            raise RuntimeError("No track positions available. Scan the CD first.")
        metadata = state.get("metadata")

        # Save metadata.json as a hidden file to avoid distracting media players
        info_path = os.path.join(output_folder, "metadata.json")
        info_data = {
            "artist": metadata.get("artist", ""),
            "album": metadata.get("title", ""),
            "genre": metadata.get("genre", ""),
            "date": metadata.get("date", ""),
            "label": metadata.get("label", ""),
            "url": metadata.get("url", "")
        }
        try:
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(info_data, f, indent=4)
            # Set the hidden attribute (0x02) on Windows
            windll.kernel32.SetFileAttributesW(info_path, 0x02)
        except Exception as e:
            append_log(window, f"Note: Failed to save/hide metadata.json: {e}")

        # Download cover art to the output folder
        cover_path = None
        if metadata.get("cover_url"):
            try:
                cover_path = os.path.join(output_folder, "folder.jpg")
                with urlopen(metadata["cover_url"]) as response:
                    with open(cover_path, "wb") as f_cover:
                        f_cover.write(response.read())
            except Exception as e:
                append_log(window, f"Failed to download cover art: {e}")

        ext = f".{audio_format.lower()}"
        if audio_format.upper() != "WAV":
            append_log(window, f"Encoding to {audio_format} using FFmpeg.")

        for index in selected_indices:
            start = track_positions[index]
            end = track_positions[index + 1] if index + 1 < len(track_positions) else state.get("leadout", start)
            track_info = metadata["tracks"][index] if metadata and index < len(metadata.get("tracks", [])) else {"number": index + 1, "title": f"Track {index + 1}"}
            filename = safe_filename(f"{int(track_info['number']):02d} - {track_info['title']}", ext)
            path = os.path.join(output_folder, filename)
            
            is_wav = audio_format.upper() == "WAV"
            rip_path = path if is_wav else path + ".tmp.wav"
            
            append_log(window, f"Ripping {track_info['title']}...")

            def progress_callback(done, total):
                percent = int(done / total * 100)
                window["-PROGRESS-"].update(percent)

            rip_audio_track(drive_letter, start, end, rip_path, progress_callback=progress_callback)
            
            # Fetch lyrics if enabled
            lyrics = ""
            if fetch_lyrics_enabled:
                append_log(window, f"Fetching lyrics for {track_info['title']}...")
                lyrics = fetch_lyrics(metadata.get("artist", ""), track_info.get("title", ""))
                if lyrics:
                    append_log(window, f"Lyrics found for {track_info['title']}")
            
            if is_wav:
                create_file_metadata(rip_path, metadata or {}, int(track_info["number"] or index + 1), track_info["title"], audio_format, lyrics)
                append_log(window, f"Saved {path}")
            else:
                append_log(window, f"Compressing to {audio_format}...")
                success, err = convert_to_compressed(rip_path, path, metadata or {}, track_info, audio_format, cover_path)
                if os.path.exists(rip_path):
                    os.remove(rip_path)
                if not success:
                    append_log(window, f"Encoding failed: {err}")
                else:
                    append_log(window, f"Saved {path}")
        append_log(window, "Rip complete.")
    except Exception as exc:
        append_log(window, f"Rip failed: {exc}")
        append_log(window, traceback.format_exc())
    finally:
        window["-RIP-"].update(disabled=False)
        window["-SCAN-"].update(disabled=False)
        window["-LOOKUP-"].update(disabled=False)


def create_main_window(settings):
    drives = list_cdrom_drives()
    default_drive = settings.get("drive", "")
    if default_drive not in drives and drives:
        default_drive = drives[0]

    menu_def = [
        ['File', ['Exit']],
        ['Edit', ['Options']],
        ['Help', ['About']]
    ]

    col_left = [
        [sg.Text("Drive:"), sg.Combo(drives, default_value=default_drive, key="-DRIVE-", size=(6, 1)),
         sg.Button("Refresh", key="-REFRESH-"), sg.Button("Scan", key="-SCAN-")],
        [sg.Text("Disc ID:"), sg.Text("", key="-DISC-", size=(20, 1))],
        [sg.Text("Tracks:"), sg.Button("Select All", key="-SELECT-ALL-", size=(10, 1)), 
         sg.Button("Clear", key="-CLEAR-SELECTION-", size=(10, 1))],
        [sg.Listbox(values=[], size=(50, 10), key="-TRACKS-", select_mode=sg.LISTBOX_SELECT_MODE_EXTENDED)],
        [sg.Text("Manual Search", font=("Any", 10, "bold"))],
        [sg.Text("Artist:", size=(6, 1)), sg.InputText(key="-SEARCH-ARTIST-", size=(15, 1)),
         sg.Text("Album:", size=(6, 1)), sg.InputText(key="-SEARCH-ALBUM-", size=(15, 1)),
         sg.Button("Find", key="-LOOKUP-")],
        [sg.Text("MusicBrainz Lookup", font=("Any", 10, "bold"))],
        [sg.Text("URL/ID:"), sg.InputText(key="-MB-URL-", size=(25, 1)),
         sg.Button("Load", key="-MBURL-"), sg.Button("Clip", key="-MBURL-DETECT-")],
        [sg.Text("Output Folder:")],
        [sg.InputText(settings.get("output_folder", os.getcwd()), key="-OUT-", size=(40, 1)), sg.FolderBrowse()],
        [sg.Text("Format:"), 
         sg.Combo(['WAV', 'MP3', 'FLAC'], default_value=settings.get("default_format", "WAV"), key="-FORMAT-", readonly=True, size=(8, 1)),
         sg.Button("Rip", key="-RIP-", size=(10, 1)), 
         sg.Button("Exit", size=(10, 1))],
    ]

    col_right = [
        [sg.Text("Metadata (Editable)", font=("Any", 12, "bold"))],
        [sg.Image(key="-COVER-", size=(200, 200), background_color="grey"),
         sg.Button("Add Image", key="-ADD-IMAGE-")],
        [sg.Text("Band:", size=(10, 1)), sg.InputText("", key="-ARTIST-", size=(40, 1))],
        [sg.Text("Album:", size=(10, 1)), sg.InputText("", key="-ALBUM-", size=(40, 1))],
        [sg.Text("Genre:", size=(10, 1)), sg.InputText("", key="-GENRE-", size=(40, 1))],
        [sg.Text("Date:", size=(10, 1)), sg.InputText("", key="-YEAR-", size=(40, 1))],
        [sg.Text("Label:", size=(10, 1)), sg.InputText("", key="-LABEL-", size=(40, 1))],
        [sg.Text("MB URL:", size=(10, 1)), sg.InputText("", key="-MB-URL-DISPLAY-", size=(32, 1), readonly=True),
         sg.Button("Go", key="-NAVIGATE-")],
        [sg.ProgressBar(100, orientation="h", size=(52, 20), key="-PROGRESS-")],
        [sg.Button("Open Folder", key="-OPEN-FOLDER-", size=(12, 1))],
        [sg.Multiline("", size=(55, 12), key="-LOG-", disabled=True, autoscroll=True)],
    ]

    cd_player_section = [
        [sg.Text("CD Player", font=("Any", 10, "bold"))],
        [sg.Button("Play Track", key="-PLAY-TRACK-", size=(12, 1)),
         sg.Button("Play All", key="-PLAY-ALL-", size=(12, 1)),
         sg.Button("Stop", key="-STOP-PLAY-", size=(12, 1))],
        [sg.Button("< Previous", key="-PREV-TRACK-", size=(12, 1)),
         sg.Button("Next >", key="-NEXT-TRACK-", size=(12, 1)),
         sg.Button("Eject", key="-EJECT-DRIVE-", size=(12, 1))],
        [sg.Text("Current:", size=(10, 1)), sg.Text("", key="-PLAYBACK-STATUS-", size=(40, 1))],
        [sg.Button("Show Lyrics", key="-SHOW-LYRICS-", size=(12, 1))],
    ]

    layout = [
        [sg.Menu(menu_def, key='-MENU-')],
        [sg.Text("CD Ripper", font=("Any", 16))],
        [sg.Column(col_left, vertical_alignment='top'), sg.VSeparator(), sg.Column(col_right, vertical_alignment='top')],
        [sg.Frame("", cd_player_section, expand_x=True)]
    ]
    return sg.Window("Audio Archivist", layout, finalize=True)


def main():
    try:
        ensure_dependencies()
    except RuntimeError as exc:
        print(exc)
        return

    settings = load_settings()
    window = create_main_window(settings)
    state = {
        "selected_drive": None,
        "disc_id": None,
        "track_positions": [],
        "track_lengths": [],
        "leadout": 0,
        "metadata": None,
        "playback_thread": None,
        "stop_playback": False,
        "current_track_index": 0,
        "current_lyrics": "",
    }

    while True:
        event, values = window.read()
        if event in (sg.WIN_CLOSED, "Exit"):
            if values is not None:
                settings["drive"] = values.get("-DRIVE-", settings.get("drive", ""))
                settings["output_folder"] = values.get("-OUT-", settings.get("output_folder", ""))
            save_settings(settings)
            break

        if event == "Options":
            opt_layout = [
                [sg.Text("Root Destination Folder:")],
                [sg.Input(settings.get("root_destination", settings.get("output_folder", os.path.join(os.path.expanduser("~"), "Music"))), key="-OPT-ROOT-"), sg.FolderBrowse()],
                [sg.Text("Subfolder Pattern (Variables: {Artist}, {Album}):")],
                [sg.Input(settings.get("subfolder_pattern", "{Artist}\\{Album}"), key="-OPT-PATTERN-")],
                [sg.Text("Default Audio Format:")],
                [sg.Combo(['WAV', 'MP3', 'FLAC'], default_value=settings.get("default_format", "WAV"), key="-OPT-FORMAT-", readonly=True)],
                [sg.Checkbox("Fetch Lyrics During Ripping", default=settings.get("fetch_lyrics", False), key="-OPT-LYRICS-")],
                [sg.Text("Example: {Artist}\\{Album} or {Artist} - {Album}", font=("Any", 8, "italic"))],
                [sg.Button("Save"), sg.Button("Cancel")]
            ]
            opt_win = sg.Window("Options", opt_layout)
            o_event, o_values = opt_win.read(close=True)
            if o_event == "Save":
                settings["root_destination"] = o_values["-OPT-ROOT-"]
                settings["subfolder_pattern"] = o_values["-OPT-PATTERN-"]
                settings["default_format"] = o_values["-OPT-FORMAT-"]
                settings["fetch_lyrics"] = o_values["-OPT-LYRICS-"]
                save_settings(settings)
                
                # Update main window with new default format
                window["-FORMAT-"].update(value=settings["default_format"])
                
                # Update output folder on main screen based on new root/pattern settings
                current_artist = values.get("-ARTIST-", "")
                current_album = values.get("-ALBUM-", "")
                new_path = get_formatted_output_path(
                    settings["root_destination"],
                    settings["subfolder_pattern"],
                    current_artist,
                    current_album
                )
                window["-OUT-"].update(new_path)

        if event == "About":
            sg.Popup("CD Ripper", "Version 0.1", "A simple tool to rip audio CDs with MusicBrainz integration.")

        if event == "-REFRESH-":
            drives = list_cdrom_drives()
            window["-DRIVE-"].update(values=drives)
            append_log(window, f"Discovered drives: {', '.join(drives) if drives else 'none'}")

        if event == "-SCAN-":
            drive = values.get("-DRIVE-")
            if not drive:
                append_log(window, "Select a drive before scanning.")
                continue
            state["selected_drive"] = drive
            window["-RIP-"].update(disabled=True)
            if scan_drive(window, drive, state):
                append_log(window, "Disc scan succeeded.")
                metadata = None
                mb_url_text = values.get("-MB-URL-", "").strip()
                if mb_url_text:
                    release_id = parse_musicbrainz_id(mb_url_text)
                    if release_id:
                        try:
                            release = get_musicbrainz_release_by_id(release_id)
                            metadata = parse_release_metadata(release)
                            append_log(window, f"Loaded metadata from MusicBrainz release {release_id}.")
                        except Exception as exc:
                            append_log(window, f"MusicBrainz URL lookup failed: {exc}")
                    else:
                        append_log(window, "MB release URL field contains invalid MusicBrainz ID.")

                if metadata is None:
                    try:
                        toc_str = build_toc_string(state.get("track_positions", []), state.get("leadout", 0))
                        append_log(window, f"Attempting disc lookup: disc_id={state['disc_id']}, toc={toc_str}")
                        release = get_musicbrainz_release_by_discid(state["disc_id"], state.get("track_positions"), state.get("leadout"))
                        
                        if release and "id" in release:
                            try:
                                # Upgrade to full metadata for tags/genres
                                release = get_musicbrainz_release_by_id(release["id"])
                            except Exception:
                                pass # Fallback to shallow metadata if full lookup fails
                                
                        metadata = parse_release_metadata(release)
                        append_log(window, f"Loaded metadata: {metadata['artist']} - {metadata['title']}")
                    except Exception as exc:
                        append_log(window, f"MusicBrainz metadata lookup failed: {exc}")
                        try:
                            metadata = query_cddb_release(state["disc_id"], state.get("track_positions"), state.get("leadout"))
                            append_log(window, f"Loaded metadata from CDDB fallback: {metadata['artist']} - {metadata['title']}")
                        except Exception as cddb_exc:
                            append_log(window, f"CDDB fallback failed: {cddb_exc}")
                            update_track_display(window, None, state["track_positions"])

                if metadata is not None:
                    update_ui_with_metadata(window, metadata, state, settings)
                window["-RIP-"].update(disabled=False)

        if event == "-LOOKUP-":
            artist = values.get("-SEARCH-ARTIST-").strip()
            album = values.get("-SEARCH-ALBUM-").strip()
            if not artist or not album:
                append_log(window, "Enter artist and album for manual search.")
                continue
            metadata = search_by_text(window, artist, album)
            if metadata:
                update_ui_with_metadata(window, metadata, state, settings)
                append_log(window, "Manual MusicBrainz search succeeded.")
                window["-RIP-"].update(disabled=False)

        if event == "-MBURL-":
            url = values.get("-MB-URL-", "").strip()
            release_id = parse_musicbrainz_id(url)
            if not release_id:
                append_log(window, "Enter a valid MusicBrainz release URL or MBID.")
                continue
            try:
                release = get_musicbrainz_release_by_id(release_id)
                metadata = parse_release_metadata(release)
                update_ui_with_metadata(window, metadata, state, settings)
                append_log(window, f"Loaded metadata from MusicBrainz release {release_id}.")
                window["-RIP-"].update(disabled=False)
            except Exception as exc:
                append_log(window, f"MusicBrainz URL lookup failed: {exc}")

        if event == "-MBURL-DETECT-":
            detected = detect_clipboard_musicbrainz_release(window)
            if not detected:
                append_log(window, "No MusicBrainz release URL or MBID found on clipboard.")
                continue
            url, release_id = detected
            window["-MB-URL-"].update(url)
            append_log(window, f"Detected MusicBrainz release ID {release_id} from clipboard.")
            try:
                release = get_musicbrainz_release_by_id(release_id)
                metadata = parse_release_metadata(release)
                update_ui_with_metadata(window, metadata, state, settings)
                append_log(window, f"Loaded metadata from MusicBrainz release {release_id}.")
                window["-RIP-"].update(disabled=False)
            except Exception as exc:
                append_log(window, f"MusicBrainz clipboard lookup failed: {exc}")

        if event == "-NAVIGATE-":
            url = values.get("-MB-URL-DISPLAY-")
            if url:
                webbrowser.open(url)

        if event == "-ADD-IMAGE-":
            image_path = sg.popup_get_file("Select Cover Image", file_types=(("Images", "*.jpg *.jpeg *.png *.bmp"),), no_window=True)
            if image_path:
                if state["metadata"] is None:
                    state["metadata"] = parse_release_metadata(None)
                state["metadata"]["manual_cover_path"] = image_path
                update_cover_display(window, image_path, is_path=True)
                append_log(window, f"Manual cover art selected: {image_path}")

        if event == "-OPEN-FOLDER-":
            output_folder = values.get("-OUT-")
            if output_folder and os.path.isdir(output_folder):
                os.startfile(output_folder)
            else:
                append_log(window, "Output folder does not exist or not selected.")

        if event == "-SELECT-ALL-":
            all_tracks = window["-TRACKS-"].get_list_values()
            window["-TRACKS-"].set_value(all_tracks)

        if event == "-CLEAR-SELECTION-":
            window["-TRACKS-"].set_value([])

        if event == "-RIP-":
            output_folder = values.get("-OUT-")
            if not output_folder:
                append_log(window, "Choose a valid output folder first.")
                continue
            
            output_folder = os.path.abspath(output_folder)
            os.makedirs(output_folder, exist_ok=True)

            # Sync state metadata with current UI values before ripping
            if state["metadata"] is None:
                state["metadata"] = {"tracks": []}
            
            state["metadata"].update({
                "artist": values["-ARTIST-"],
                "title": values["-ALBUM-"],
                "genre": values["-GENRE-"],
                "date": values["-YEAR-"],
                "label": values["-LABEL-"],
                "url": values["-MB-URL-DISPLAY-"]
            })

            selected_tracks = values["-TRACKS-"]
            if not selected_tracks:
                # If nothing is selected, default to all tracks
                selected_indices = list(range(len(state.get("track_positions", []))))
                if not selected_indices:
                    append_log(window, "No tracks found to rip. Scan the CD first.")
                    continue
            else:
                # Map selected display strings back to their indices
                all_tracks = window["-TRACKS-"].get_list_values()
                selected_indices = [i for i, t in enumerate(all_tracks) if t in selected_tracks]

            audio_format = values["-FORMAT-"]
            fetch_lyrics_enabled = settings.get("fetch_lyrics", False)
            window["-RIP-"].update(disabled=True)
            window["-SCAN-"].update(disabled=True)
            window["-LOOKUP-"].update(disabled=True)
            thread = threading.Thread(target=rip_thread, args=(window, state, output_folder, selected_indices, audio_format, fetch_lyrics_enabled), daemon=True)
            thread.start()

        if event == "-PLAY-TRACK-":
            drive = state.get("selected_drive")
            track_positions = state.get("track_positions", [])
            metadata = state.get("metadata")
            
            if not drive:
                append_log(window, "No drive selected. Scan a CD first.")
                continue
            if not track_positions:
                append_log(window, "No tracks found. Scan the CD first.")
                continue
            
            selected_tracks = values["-TRACKS-"]
            if not selected_tracks:
                append_log(window, "Select a track to play.")
                continue
            
            all_tracks = window["-TRACKS-"].get_list_values()
            track_index = 0
            for i, t in enumerate(all_tracks):
                if t in selected_tracks:
                    track_index = i
                    break
            
            state["current_track_index"] = track_index
            start = track_positions[track_index]
            end = track_positions[track_index + 1] if track_index + 1 < len(track_positions) else state.get("leadout", start)
            
            track_info = metadata["tracks"][track_index] if metadata and track_index < len(metadata.get("tracks", [])) else {"number": track_index + 1, "title": f"Track {track_index + 1}"}
            track_title = track_info.get("title", f"Track {track_index + 1}")
            
            # Fetch lyrics before playing
            artist = metadata.get("artist", "") if metadata else ""
            lyrics = fetch_lyrics(artist, track_title)
            state["current_lyrics"] = lyrics
            
            window["-PLAYBACK-STATUS-"].update(f"Playing: {track_title}")
            state["stop_playback"] = False
            thread = threading.Thread(target=play_cd_track_async, args=(drive, start, end, track_title, window, state), daemon=True)
            state["playback_thread"] = thread
            thread.start()

        if event == "-PLAY-ALL-":
            drive = state.get("selected_drive")
            track_positions = state.get("track_positions", [])
            metadata = state.get("metadata")
            
            if not drive:
                append_log(window, "No drive selected. Scan a CD first.")
                continue
            if not track_positions or len(track_positions) < 2:
                append_log(window, "No tracks found. Scan the CD first.")
                continue
            
            def play_all_tracks():
                try:
                    for track_index in range(len(track_positions) - 1):
                        if state.get("stop_playback"):
                            append_log(window, "Playback stopped.")
                            return
                        
                        state["current_track_index"] = track_index
                        start = track_positions[track_index]
                        end = track_positions[track_index + 1] if track_index + 1 < len(track_positions) else state.get("leadout", start)
                        
                        track_info = metadata["tracks"][track_index] if metadata and track_index < len(metadata.get("tracks", [])) else {"number": track_index + 1, "title": f"Track {track_index + 1}"}
                        track_title = track_info.get("title", f"Track {track_index + 1}")
                        
                        # Fetch lyrics for this track
                        artist = metadata.get("artist", "") if metadata else ""
                        lyrics = fetch_lyrics(artist, track_title)
                        state["current_lyrics"] = lyrics
                        
                        window["-PLAYBACK-STATUS-"].update(f"Playing: {track_title} ({track_index + 1}/{len(track_positions) - 1})")
                        play_cd_track_async(drive, start, end, track_title, window, state)
                    
                    window["-PLAYBACK-STATUS-"].update("Playback complete.")
                except Exception as exc:
                    append_log(window, f"Playback error: {exc}")
            
            window["-PLAYBACK-STATUS-"].update("Starting playback of all tracks...")
            state["stop_playback"] = False
            thread = threading.Thread(target=play_all_tracks, daemon=True)
            state["playback_thread"] = thread
            thread.start()

        if event == "-STOP-PLAY-":
            state["stop_playback"] = True
            window["-PLAYBACK-STATUS-"].update("Stopping...")
            append_log(window, "Playback stop requested.")

        if event == "-EJECT-DRIVE-":
            drive = state.get("selected_drive")
            if not drive:
                append_log(window, "No drive selected.")
                continue
            
            if eject_cd_drive(drive):
                append_log(window, f"Drive {drive} ejected.")
                window["-PLAYBACK-STATUS-"].update("CD ejected")
            else:
                append_log(window, f"Failed to eject drive {drive}.")

        if event == "-PREV-TRACK-":
            track_positions = state.get("track_positions", [])
            if not track_positions:
                append_log(window, "No tracks available. Scan the CD first.")
                continue
            
            all_tracks = window["-TRACKS-"].get_list_values()
            current_index = state.get("current_track_index", 0)
            
            if current_index > 0:
                prev_index = current_index - 1
                state["current_track_index"] = prev_index
                window["-TRACKS-"].set_value([all_tracks[prev_index]])
                append_log(window, f"Navigated to previous track (Track {prev_index + 1}).")
            else:
                append_log(window, "Already at the first track.")

        if event == "-NEXT-TRACK-":
            track_positions = state.get("track_positions", [])
            if not track_positions:
                append_log(window, "No tracks available. Scan the CD first.")
                continue
            
            all_tracks = window["-TRACKS-"].get_list_values()
            current_index = state.get("current_track_index", 0)
            
            if current_index < len(all_tracks) - 1:
                next_index = current_index + 1
                state["current_track_index"] = next_index
                window["-TRACKS-"].set_value([all_tracks[next_index]])
                append_log(window, f"Navigated to next track (Track {next_index + 1}).")
            else:
                append_log(window, "Already at the last track.")

        if event == "-SHOW-LYRICS-":
            lyrics = state.get("current_lyrics", "")
            metadata = state.get("metadata")
            track_index = state.get("current_track_index", 0)
            
            track_info = None
            if metadata and track_index < len(metadata.get("tracks", [])):
                track_info = metadata["tracks"][track_index]
                track_title = track_info.get("title", f"Track {track_index + 1}")
            else:
                track_title = f"Track {track_index + 1}"
            
            if not lyrics:
                append_log(window, "No lyrics available for this track.")
                continue
            
            # Display lyrics in a separate window
            show_lyrics_window(track_title, lyrics)

    window.close()


if __name__ == "__main__":
    main()
