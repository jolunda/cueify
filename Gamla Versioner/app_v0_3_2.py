import streamlit as st
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from pathlib import Path
import html
import json
import re
import time

# Cueify styr en Spotify-enhet via Web API. Själva ljudet spelas i Spotify-appen
# (dator, telefon, högtalare) som är vald som enhet.

APP_VERSION = "0.3.2"
MAX_TRACKS = 20
CUES_FILE = Path(__file__).parent / "cues.json"
TOKEN_CACHE = Path(__file__).parent / ".cache"
SCOPE = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-read-collaborative"
])

# Mellanslag som kommer tätare än så här räknas som studs och ignoreras
SPACE_DEBOUNCE_SECONDS = 0.6

# En Spotify-app som inte spelat något sedan start tar emot kommandon men ignorerar play
DEVICE_IDLE_HINT = (
    "Spela valfri låt en kort stund direkt i Spotify-appen på enheten "
    "(eller starta om Spotify) och tryck mellanslag igen."
)

st.set_page_config(page_title="Cueify", page_icon="🎵", layout="wide")


# =====================================
# SPOTIFY
# =====================================

@st.cache_resource
def get_spotify():
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=st.secrets["SPOTIFY_CLIENT_ID"],
            client_secret=st.secrets["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=st.secrets.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
            scope=SCOPE,
            cache_path=str(TOKEN_CACHE)
        )
    )


sp = get_spotify()


def playlist_id_from_link(link):

    link = link.strip()

    # Klarar både webblänk (open.spotify.com/playlist/ID?si=...) och URI (spotify:playlist:ID)
    match = re.search(r"playlist[/:]([A-Za-z0-9]+)", link)

    if match:
        return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9]{22}", link):
        return link

    return None


def load_playlist(playlist_id):

    meta = sp.playlist(playlist_id, fields="name,owner.display_name")

    # Nyare API-versioner använder /items och fältet "item", äldre /tracks och "track"
    try:
        page = sp._get(f"playlists/{playlist_id}/items", limit=MAX_TRACKS, additional_types="track")
    except spotipy.SpotifyException:
        page = sp.playlist_items(playlist_id, limit=MAX_TRACKS, additional_types=("track",))

    tracks = []

    for entry in page["items"]:

        track = entry.get("item") or entry.get("track")

        # Lokala filer och borttagna låtar går inte att spela via API:t
        if not track or not track.get("uri") or track.get("is_local"):
            continue

        tracks.append({
            "uri": track["uri"],
            "name": track["name"],
            "artist": ", ".join(a["name"] for a in track["artists"]),
            "duration_ms": track["duration_ms"]
        })

    return meta["name"], tracks


def get_devices():
    try:
        return sp.devices()["devices"]
    except spotipy.SpotifyException as error:
        st.error(f"Kunde inte hämta Spotify-enheter: {error}")
        return []


def pause(device_id):

    # Spotify svarar "Restriction violated" när spelaren redan står still,
    # t.ex. om låten tagit slut eller pausats direkt i Spotify. Målet är då redan nått.
    try:
        sp.pause_playback(device_id=device_id)
    except spotipy.SpotifyException as error:
        if "restriction violated" not in str(error).lower():
            raise


def wait_until_playing(timeout=2.0):

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        state = sp.current_playback()
        if state and state.get("is_playing"):
            return True
        time.sleep(0.15)

    return False


# =====================================
# TIDER
# =====================================

def time_to_ms(text):

    # Tillåter "1:23", "1:23.5", "83" och "83.5"
    text = str(text).strip().replace(",", ".")

    if not text:
        return 0

    match = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", text)

    if not match:
        return None

    mins = int(match.group(1) or 0)
    secs = float(match.group(2))

    return int((mins * 60 + secs) * 1000)


def ms_to_time(ms):

    secs = ms / 1000
    mins = int(secs // 60)
    rest = secs - mins * 60

    if abs(rest - round(rest)) < 0.05:
        return f"{mins}:{int(round(rest)):02d}"

    return f"{mins}:{rest:04.1f}"


# =====================================
# SPARADE CUE-TIDER
# =====================================

def read_cues():
    if not CUES_FILE.exists():
        return {}
    return json.loads(CUES_FILE.read_text(encoding="utf-8"))


def save_cues(playlist_id, starts):
    cues = read_cues()
    cues[playlist_id] = starts
    CUES_FILE.write_text(json.dumps(cues, indent=2, ensure_ascii=False), encoding="utf-8")


# =====================================
# SHOWLÄGE
# =====================================

def prep_current():

    # Förladdar aktuell låt pausad på starttiden med volym 0, så att GO bara behöver
    # återuppta uppspelningen. Kräver att enheten tillåter volymstyrning.
    show = st.session_state["show"]
    cue = show["cues"][show["index"]]
    device_id = show["device_id"]

    show["prepped"] = None

    if not show["preload"]:
        return

    # Volymen återställs alltid till nivån från showstarten, så att ett misslyckat
    # försök aldrig lämnar kvar volym 0 till nästa
    try:
        sp.volume(0, device_id=device_id)
        sp.start_playback(device_id=device_id, uris=[cue["uri"]], position_ms=cue["start_ms"])

        # Pausas den innan enheten hunnit starta svarar Spotify med fel
        if wait_until_playing():
            pause(device_id)
            sp.seek_track(cue["start_ms"], device_id=device_id)
            show["prepped"] = cue["uri"]
        else:
            show["message"] = f"Förladdning misslyckades. {DEVICE_IDLE_HINT}"

    except spotipy.SpotifyException as error:
        show["message"] = f"Förladdning misslyckades, låten startas direkt vid GO ({error.msg})"

    finally:
        try:
            sp.volume(show["volume"], device_id=device_id)
        except spotipy.SpotifyException:
            pass


def on_space():

    show = st.session_state["show"]

    now = time.monotonic()
    if now - show["last_space"] < SPACE_DEBOUNCE_SECONDS:
        return
    show["last_space"] = now

    device_id = show["device_id"]
    show["message"] = ""

    try:

        if show["playing"]:

            pause(device_id)
            show["playing"] = False

            if show["index"] < len(show["cues"]) - 1:
                show["index"] += 1
                prep_current()
            else:
                show["finished"] = True

        elif not show["finished"]:

            cue = show["cues"][show["index"]]

            if show["prepped"] == cue["uri"]:
                sp.start_playback(device_id=device_id)
            else:
                sp.start_playback(device_id=device_id, uris=[cue["uri"]], position_ms=cue["start_ms"])

            # Spotify svarar OK även när enheten struntar i kommandot, så kontrollera
            # att den faktiskt spelar innan showen visar SPELAR
            if wait_until_playing():
                show["playing"] = True
            else:
                show["prepped"] = None
                show["message"] = f"Spotify tog emot GO men enheten startade inte. {DEVICE_IDLE_HINT}"

    except spotipy.SpotifyException as error:
        show["message"] = f"Spotify svarade med fel: {error.msg}"


def enter_show(cues, device_id, preload, volume):

    st.session_state["show"] = {
        "cues": cues,
        "device_id": device_id,
        "preload": preload,
        "index": 0,
        "playing": False,
        "finished": False,
        "prepped": None,
        "volume": volume or 100,
        "last_space": 0.0,
        "message": ""
    }

    # Väcker enheten så att första GO inte fastnar på "ingen aktiv enhet"
    try:
        sp.transfer_playback(device_id, force_play=False)
    except spotipy.SpotifyException:
        pass

    prep_current()


def exit_show():

    show = st.session_state.pop("show")

    if show["playing"]:
        try:
            pause(show["device_id"])
        except spotipy.SpotifyException:
            pass


def render_show():

    show = st.session_state["show"]
    cues = show["cues"]
    cue = cues[show["index"]]

    st.markdown(
        """
        <style>
        .cue-card { padding: 2rem; border-radius: 1rem; margin-bottom: 1rem; color: #fff; }
        .cue-ready { background: #1f6f3f; }
        .cue-playing { background: #b3261e; }
        .cue-done { background: #444; }
        .cue-state { font-size: 1.2rem; letter-spacing: 0.15em; opacity: 0.85; }
        .cue-title { font-size: 3rem; font-weight: 700; line-height: 1.1; margin: 0.5rem 0; }
        .cue-meta { font-size: 1.4rem; opacity: 0.9; }
        .cue-row { padding: 0.35rem 0.75rem; border-radius: 0.4rem; }
        .cue-row-current { background: rgba(127,127,127,0.25); font-weight: 700; }
        .cue-row-past { opacity: 0.45; }
        </style>
        """,
        unsafe_allow_html=True
    )

    if show["finished"]:
        state_class, state_text, hint = "cue-done", "SLUT", "Alla cues är spelade"
    elif show["playing"]:
        state_class, state_text, hint = "cue-playing", "SPELAR", "Mellanslag = STOPP och ladda nästa"
    else:
        prep_note = " · förladdad" if show["prepped"] == cue["uri"] else ""
        state_class, state_text, hint = "cue-ready", f"KLAR{prep_note}", "Mellanslag = GO"

    st.markdown(
        f"""
        <div class="cue-card {state_class}">
            <div class="cue-state">CUE {show['index'] + 1} / {len(cues)} · {state_text}</div>
            <div class="cue-title">{html.escape(cue['name'])}</div>
            <div class="cue-meta">{html.escape(cue['artist'])} · start {ms_to_time(cue['start_ms'])}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.button(
        f"␣  {hint}",
        key="space_btn",
        on_click=on_space,
        shortcut="Space",
        type="primary",
        width="stretch",
        disabled=show["finished"]
    )

    if show["message"]:
        st.warning(show["message"])

    rows = []

    for i, c in enumerate(cues):

        if i == show["index"]:
            row_class = "cue-row cue-row-current"
        elif i < show["index"]:
            row_class = "cue-row cue-row-past"
        else:
            row_class = "cue-row"

        rows.append(
            f'<div class="{row_class}">{i + 1}. {html.escape(c["name"])} – '
            f'{html.escape(c["artist"])} <span style="float:right">{ms_to_time(c["start_ms"])}</span></div>'
        )

    st.markdown("".join(rows), unsafe_allow_html=True)

    st.divider()

    # Upplåsningen kräver två steg så att den inte går att trigga av misstag under show
    with st.expander("🔒 Lås upp"):
        confirm = st.checkbox("Jag vill lämna showläget")
        st.button("Avsluta showläge", on_click=exit_show, disabled=not confirm)


# =====================================
# GUI
# =====================================

if "show" in st.session_state:
    render_show()
    st.stop()

st.title(f"Cueify v{APP_VERSION}")

link = st.text_input("Länk till Spotify-spellista", placeholder="https://open.spotify.com/playlist/...")

if st.button("Hämta spellista"):

    playlist_id = playlist_id_from_link(link)

    if playlist_id is None:
        st.error("Känner inte igen länken som en Spotify-spellista.")

    else:
        try:
            name, tracks = load_playlist(playlist_id)
            st.session_state["playlist"] = {"id": playlist_id, "name": name, "tracks": tracks}
        except spotipy.SpotifyException as error:
            st.error(f"Kunde inte hämta spellistan: {error.msg}")

if "playlist" not in st.session_state:
    st.stop()

playlist = st.session_state["playlist"]
tracks = playlist["tracks"]

st.subheader(playlist["name"])

if not tracks:
    st.warning("Spellistan innehåller inga spelbara låtar.")
    st.stop()

st.caption(f"Visar de första {MAX_TRACKS} låtarna. Ange starttid som m:ss, t.ex. 1:23 eller 1:23.5.")

saved = read_cues().get(playlist["id"], {})

edited = st.data_editor(
    [
        {
            "Låt": t["name"],
            "Artist": t["artist"],
            "Längd": ms_to_time(t["duration_ms"]),
            "Start": saved.get(t["uri"], "0:00")
        }
        for t in tracks
    ],
    column_config={
        "Låt": st.column_config.TextColumn(disabled=True),
        "Artist": st.column_config.TextColumn(disabled=True),
        "Längd": st.column_config.TextColumn(disabled=True, width="small"),
        "Start": st.column_config.TextColumn(width="small")
    },
    hide_index=False,
    width="stretch",
    key=f"editor_{playlist['id']}"
)

cues = []
errors = []

for track, row in zip(tracks, edited):

    start_ms = time_to_ms(row["Start"])

    if start_ms is None:
        errors.append(f"{track['name']}: \"{row['Start']}\" är ingen giltig tid")
    elif start_ms >= track["duration_ms"]:
        errors.append(f"{track['name']}: starttiden är efter låtens slut")
    else:
        cues.append({**track, "start_ms": start_ms})

for error in errors:
    st.error(error)

starts = {t["uri"]: row["Start"] for t, row in zip(tracks, edited)}

if starts != saved:
    save_cues(playlist["id"], starts)

st.divider()

devices = get_devices()

if st.button("🔄 Uppdatera enheter"):
    st.rerun()

if not devices:
    st.info("Ingen Spotify-enhet hittades. Öppna Spotify på den dator eller högtalare som ska spela.")
    st.stop()

device_names = [f"{d['name']} ({d['type']})" for d in devices]

active_index = next((i for i, d in enumerate(devices) if d["is_active"]), 0)

device_choice = st.radio("Spotify-enhet", device_names, index=active_index)
device = devices[device_names.index(device_choice)]

preload = st.checkbox(
    "Förladda nästa låt pausad i bakgrunden",
    help=(
        "Laddar låten på starttiden med volym 0 och pausar, så att GO startar snabbare. "
        "Fungerar bara på enheter som tillåter volymstyrning (t.ex. Spotify på datorn)."
    ),
    disabled=not device.get("supports_volume", True)
)

if st.button("🔒 Starta showläge", type="primary", disabled=bool(errors)):
    enter_show(cues, device["id"], preload, device.get("volume_percent"))
    st.rerun()
