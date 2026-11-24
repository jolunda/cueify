import streamlit as st
import spotipy
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError
from spotipy.cache_handler import CacheHandler
from pathlib import Path
import base64
import hashlib
import hmac
import html
import json
import re
import requests
import secrets
import time

# Cueify styr en Spotify-enhet via Web API. Själva ljudet spelas i Spotify-appen
# (dator, telefon, högtalare) som är vald som enhet.

# Varje besökare loggar in med sitt eget Spotify-konto via appens Client ID. Inloggningen
# finns bara i besökarens session och sparas aldrig på disk eller i datarepot.
# Spotify släpper bara in konton vars e-post finns under User Management i dashboarden.

APP_VERSION = "0.5.0"
MAX_TRACKS = 20
LOCAL_CUES_DIR = Path(__file__).parent / "cues"
SCOPE = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-read-collaborative"
])

# Hur länge en påbörjad inloggning hos Spotify får ta innan den räknas som ogiltig
LOGIN_STATE_SECONDS = 600

# Mellanslag som kommer tätare än så här räknas som studs och ignoreras
SPACE_DEBOUNCE_SECONDS = 0.6

# En Spotify-app som inte spelat något sedan start tar emot kommandon men ignorerar play
DEVICE_IDLE_HINT = (
    "Spela valfri låt en kort stund direkt i Spotify-appen på enheten "
    "(eller starta om Spotify) och tryck mellanslag igen."
)

st.set_page_config(page_title="Cueify", page_icon="🎵", layout="wide")


# =====================================
# INLOGGNING
# =====================================

class SessionCacheHandler(CacheHandler):

    # Spotipy sparar och förnyar token här, så att den följer besökarens session
    def get_cached_token(self):
        return st.session_state.get("token")

    def save_token_to_cache(self, token_info):
        st.session_state["token"] = token_info


def get_auth():

    if "auth" not in st.session_state:
        st.session_state["auth"] = SpotifyOAuth(
            client_id=st.secrets["SPOTIFY_CLIENT_ID"],
            client_secret=st.secrets["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=st.secrets["SPOTIFY_REDIRECT_URI"],
            scope=SCOPE,
            cache_handler=SessionCacheHandler(),
            open_browser=False
        )

    return st.session_state["auth"]


def sign(text):
    return hmac.new(
        st.secrets["SPOTIFY_CLIENT_SECRET"].encode("utf-8"),
        text.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()[:32]


def make_login_state():

    # Sessionen byts ut när Spotify skickar tillbaka besökaren, så state-värdet
    # kontrolleras med en signatur i stället för att sparas i sessionen
    payload = f"{secrets.token_urlsafe(16)}.{int(time.time())}"
    return f"{payload}.{sign(payload)}"


def valid_login_state(state):

    try:
        nonce, issued, signature = state.split(".")
        issued = int(issued)
    except (AttributeError, ValueError):
        return False

    return (
        hmac.compare_digest(signature, sign(f"{nonce}.{issued}"))
        and time.time() - issued < LOGIN_STATE_SECONDS
    )


def handle_login_callback():

    params = st.query_params

    if "code" not in params and "error" not in params:
        return

    code = params.get("code")
    state = params.get("state")
    error = params.get("error")

    st.query_params.clear()

    if error:
        st.session_state["login_error"] = "Inloggningen avbröts hos Spotify."
    elif not valid_login_state(state):
        st.session_state["login_error"] = "Inloggningen kunde inte verifieras. Försök igen."
    else:
        try:
            get_auth().get_access_token(code, as_dict=False, check_cache=False)
        except SpotifyOauthError as oauth_error:
            st.session_state["login_error"] = f"Spotify godkände inte inloggningen ({oauth_error.error_description or oauth_error})."


def logout():
    for key in ("token", "user", "cues", "cues_sha", "playlist", "show"):
        st.session_state.pop(key, None)


def render_login():

    st.title("Cueify")
    st.write("Spela upp låtar från en Spotify-spellista på exakta starttider, med mellanslag som GO och STOPP.")

    if "login_error" in st.session_state:
        st.error(st.session_state.pop("login_error"))

    url = get_auth().get_authorize_url(state=make_login_state())

    # Streamlit Clouds ram får inte byta sida i hela fönstret, bara öppna nya flikar,
    # så inloggningen öppnas i en ny flik och Spotify skickar tillbaka besökaren dit
    st.link_button("Logga in med Spotify", url, type="primary")

    st.caption(
        "Inloggningen öppnas i en ny flik, fortsätt sedan i den. Du behöver Spotify Premium, "
        "och e-postadressen för ditt Spotify-konto måste vara godkänd för Cueify av den som driver appen."
    )


def login():

    handle_login_callback()

    auth = get_auth()

    if not auth.validate_token(auth.cache_handler.get_cached_token()):
        render_login()
        st.stop()

    client = spotipy.Spotify(auth_manager=auth)

    if "user" not in st.session_state:
        try:
            st.session_state["user"] = client.me()
        except spotipy.SpotifyException as error:
            logout()
            if error.http_status == 403:
                st.session_state["login_error"] = (
                    "Ditt Spotify-konto är inte godkänt för Cueify. Be den som driver appen "
                    "lägga till e-postadressen för ditt Spotify-konto."
                )
            else:
                st.session_state["login_error"] = f"Kunde inte läsa ditt Spotify-konto ({error.msg})."
            st.rerun()

    return client


sp = login()
user = st.session_state["user"]


# =====================================
# SPOTIFY
# =====================================

def spotify_error_text(error):

    if error.reason == "PREMIUM_REQUIRED":
        return "Ditt Spotify-konto behöver Premium för att Cueify ska kunna styra uppspelningen."

    return f"Spotify svarade med fel: {error.msg}"


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
        st.error(f"Kunde inte hämta Spotify-enheter: {error.msg}")
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

# Varje Spotify-konto har en egen fil. Med DATA_REPO i secrets sparas den i ett
# GitHub-repo som i Navet (Streamlit Cloud raderar lokala filer vid omstart),
# annars i mappen cues bredvid appen. Filen innehåller bara spellist-ID, låt-URI:er och tider.

def cues_file_name():
    return re.sub(r"[^A-Za-z0-9_-]", "_", user["id"]) + ".json"


def use_data_repo():
    return "DATA_REPO" in st.secrets


def data_repo_url(path):
    return f"https://api.github.com/repos/{st.secrets['DATA_REPO']}/contents/cues/{path}"


def data_repo_headers():
    return {
        "Authorization": f"Bearer {st.secrets['DATA_REPO_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def read_cues_from_repo():

    response = requests.get(data_repo_url(cues_file_name()), headers=data_repo_headers(), timeout=15)

    # Filen finns inte förrän användaren sparat första gången
    if response.status_code == 404:
        return {}, None

    response.raise_for_status()
    body = response.json()

    return json.loads(base64.b64decode(body["content"]).decode("utf-8")), body["sha"]


def write_cues_to_repo(cues):

    content = json.dumps(cues, ensure_ascii=False, indent=1, sort_keys=True) + "\n"

    for attempt in range(2):

        body = {
            "message": "Cueify: spara cue-tider",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii")
        }

        if st.session_state.get("cues_sha"):
            body["sha"] = st.session_state["cues_sha"]

        response = requests.put(data_repo_url(cues_file_name()), headers=data_repo_headers(), json=body, timeout=15)

        # Har filen sparats från en annan flik sedan den lästes, hämtas aktuell version om
        if response.status_code in (409, 422) and attempt == 0:
            st.session_state["cues_sha"] = read_cues_from_repo()[1]
            continue

        response.raise_for_status()
        st.session_state["cues_sha"] = response.json()["content"]["sha"]
        return


def read_cues():

    # Läses en gång per session och hålls sedan i minnet
    if "cues" not in st.session_state:

        if use_data_repo():
            cues, sha = read_cues_from_repo()
            st.session_state["cues_sha"] = sha
        else:
            path = LOCAL_CUES_DIR / cues_file_name()
            cues = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

        st.session_state["cues"] = cues

    return st.session_state["cues"]


def save_cues(playlist_id, starts):

    cues = {**read_cues(), playlist_id: starts}

    if use_data_repo():
        write_cues_to_repo(cues)
    else:
        LOCAL_CUES_DIR.mkdir(exist_ok=True)
        (LOCAL_CUES_DIR / cues_file_name()).write_text(
            json.dumps(cues, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    st.session_state["cues"] = cues


# =====================================
# SHOWLÄGE
# =====================================

def play_cue(show, cue):

    # Startas låten som lös URI tömmer Spotify-appen spelaren i stället för att spela,
    # så den startas alltid inifrån spellistan
    sp.start_playback(
        device_id=show["device_id"],
        context_uri=show["playlist_uri"],
        offset={"uri": cue["uri"]},
        position_ms=cue["start_ms"]
    )


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
        play_cue(show, cue)

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

            # En låt som klickats fram under uppspelningen går före nästa i listan
            chosen = show.pop("next_index", None)

            if chosen is not None:
                show["index"] = chosen
                prep_current()
            elif show["index"] < len(show["cues"]) - 1:
                show["index"] += 1
                prep_current()
            else:
                show["finished"] = True

        elif not show["finished"]:

            cue = show["cues"][show["index"]]

            if show["prepped"] == cue["uri"]:
                sp.start_playback(device_id=device_id)
            else:
                play_cue(show, cue)

            # Spotify svarar OK även när enheten struntar i kommandot, så kontrollera
            # att den faktiskt spelar innan showen visar SPELAR
            if wait_until_playing():
                show["playing"] = True
            else:
                show["prepped"] = None
                show["message"] = f"Spotify tog emot GO men enheten startade inte. {DEVICE_IDLE_HINT}"

    except spotipy.SpotifyException as error:
        show["message"] = spotify_error_text(error)


def select_cue(i):

    show = st.session_state["show"]
    show["message"] = ""

    # Under uppspelning blir den valda låten nästa, så att den spelande inte avbryts.
    # Annars ställs showen direkt på den valda låten.
    if show["playing"]:
        show["next_index"] = None if i == show["index"] else i
        return

    show["index"] = i
    show["finished"] = False
    show["next_index"] = None
    prep_current()


def enter_show(playlist_id, cues, device_id, preload, volume):

    st.session_state["show"] = {
        "playlist_uri": f"spotify:playlist:{playlist_id}",
        "cues": cues,
        "device_id": device_id,
        "preload": preload,
        "index": 0,
        "playing": False,
        "finished": False,
        "prepped": None,
        "next_index": None,
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
        </style>
        """,
        unsafe_allow_html=True
    )

    chosen = show.get("next_index")

    if show["finished"]:
        state_class, state_text, hint = "cue-done", "SLUT", "Alla cues är spelade – klicka på en låt för att fortsätta"
    elif show["playing"] and chosen is not None:
        state_class, state_text = "cue-playing", "SPELAR"
        hint = f"Mellanslag = STOPP och ladda {chosen + 1}. {cues[chosen]['name']}"
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

    st.caption(
        "Klicka på en låt för att köra den härnäst. Under uppspelning laddas den vid nästa STOPP."
    )

    for i, c in enumerate(cues):

        if i == show["index"]:
            marker = "▶"
        elif i == chosen:
            marker = "⏭ NÄSTA"
        elif i < show["index"]:
            marker = "✓"
        else:
            marker = ""

        st.button(
            f"{marker}  {i + 1}. {c['name']} – {c['artist']}  ·  {ms_to_time(c['start_ms'])}",
            key=f"cue_{i}",
            on_click=select_cue,
            args=(i,),
            type="secondary" if i in (show["index"], chosen) else "tertiary",
            width="stretch"
        )

    # En klickad knapp behåller annars fokus, och då skulle mellanslag även klicka på den
    st.html(
        """
        <script>
        if (!window.cueifyBlur) {
            window.cueifyBlur = true;
            document.addEventListener("click", (event) => {
                const button = event.target.closest("button");
                if (button) setTimeout(() => button.blur(), 0);
            }, true);
        }
        </script>
        """,
        unsafe_allow_javascript=True
    )

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

title_col, user_col = st.columns([4, 1], vertical_alignment="center")

with title_col:
    st.title(f"Cueify v{APP_VERSION}")

with user_col:
    st.caption(f"Inloggad som **{user.get('display_name') or user['id']}**")
    st.button("Logga ut", on_click=logout)

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

try:
    saved = read_cues().get(playlist["id"], {})
except requests.RequestException as error:
    st.warning(f"Kunde inte läsa sparade cue-tider ({error}). Tiderna sparas inte den här gången.")
    saved = None

edited = st.data_editor(
    [
        {
            "Låt": t["name"],
            "Artist": t["artist"],
            "Längd": ms_to_time(t["duration_ms"]),
            "Start": (saved or {}).get(t["uri"], "0:00")
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

# Kunde de sparade tiderna inte läsas skrivs inget, så att de inte ersätts med nollor
if saved is not None and starts != saved:
    try:
        save_cues(playlist["id"], starts)
    except requests.RequestException as error:
        st.warning(f"Kunde inte spara cue-tiderna ({error}).")

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
    enter_show(playlist["id"], cues, device["id"], preload, device.get("volume_percent"))
    st.rerun()
