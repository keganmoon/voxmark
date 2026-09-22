#!/usr/bin/env python3
import os
import sys
import time
import re
import xml.etree.ElementTree as ET
import zipfile
import urllib.parse
import tempfile
import subprocess
import logging
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from faster_whisper import WhisperModel
from rapidfuzz import fuzz, process

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("voxmark")

# DB Connection Config
# DB Connection Config (set via environment, no default credentials shipped)
DB_URL = os.environ.get("VOXMARK_DB_URL", "postgresql://user:password@localhost:5432/bookorbit")

# Debounce Settings
DEBOUNCE_SECONDS = 30
POLL_INTERVAL = 5

# Initialize Whisper model (Tiny model is extremely fast and light on CPU)
logger.info("Loading Faster-Whisper model...")
model = WhisperModel("tiny", device="cpu", compute_type="float32")
logger.info("Model loaded successfully.")

# Path translator: container paths -> host paths
EBOOKS_HOST_ROOT = os.environ.get("VOXMARK_EBOOKS_PATH", "/mnt/media/media/books/ebooks")
AUDIOBOOKS_HOST_ROOT = os.environ.get("VOXMARK_AUDIOBOOKS_PATH", "/mnt/media/media/books/audiobooks")

def to_host_path(container_path):
    if not container_path:
        return ""
    if container_path.startswith('/books/'):
        return container_path.replace('/books/', EBOOKS_HOST_ROOT + '/', 1)
    elif container_path.startswith('/audiobooks/'):
        return container_path.replace('/audiobooks/', AUDIOBOOKS_HOST_ROOT + '/', 1)
    return container_path

# Get DB Connection
def get_db_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

# ----------------- EPUB Parsing Helper -----------------
def parse_epub_spine_and_text(epub_path):
    """
    Parses EPUB archive to extract container.xml -> OPF manifest -> spine order -> XHTML texts.
    Returns: (spine_files, chapters_text_map)
      spine_files: list of tuples (href, full_zip_path) in reading order
      chapters_text_map: dict of full_zip_path -> text string
    """
    spine_files = []
    chapters_text_map = {}
    
    if not os.path.exists(epub_path):
        logger.error(f"EPUB file not found: {epub_path}")
        return spine_files, chapters_text_map

    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            # 1. Parse container.xml
            container_xml = z.read('META-INF/container.xml')
            root = ET.fromstring(container_xml)
            ns = {'ns': 'urn:oasis:names:tc:opendocument:xmlns:container'}
            rootfile = root.find('.//ns:rootfile', ns)
            if rootfile is None:
                return spine_files, chapters_text_map
            opf_path = rootfile.attrib['full-path']
            opf_dir = opf_path.rsplit('/', 1)[0] if '/' in opf_path else ''

            # 2. Parse OPF file
            opf_xml = z.read(opf_path)
            opf_root = ET.fromstring(opf_xml)
            # Handle default namespace if present
            opf_ns_uri = opf_root.tag.split('}')[0].strip('{') if '}' in opf_root.tag else 'http://www.idpf.org/2007/opf'
            opf_ns = {'opf': opf_ns_uri}
            
            # Map manifest items
            manifest = {}
            for item in opf_root.findall('.//opf:manifest/opf:item', opf_ns):
                manifest[item.attrib['id']] = item.attrib['href']
            
            # Read spine order
            for itemref in opf_root.findall('.//opf:spine/opf:itemref', opf_ns):
                idref = itemref.attrib['idref']
                href = manifest.get(idref)
                if href:
                    href_decoded = urllib.parse.unquote(href)
                    chapter_path = f"{opf_dir}/{href_decoded}" if opf_dir else href_decoded
                    spine_files.append((href_decoded, chapter_path))
                    
            # 3. Read chapter text content
            for _, chapter_path in spine_files:
                try:
                    chapter_html = z.read(chapter_path).decode('utf-8', errors='ignore')
                    # Strip tags and normalize spacing
                    text = re.sub(r'<[^>]+>', ' ', chapter_html)
                    text = html_unescape(text)
                    text = re.sub(r'\s+', ' ', text).strip()
                    chapters_text_map[chapter_path] = text
                except Exception as e:
                    logger.error(f"Error reading chapter {chapter_path} in EPUB: {e}")
    except Exception as e:
        logger.error(f"Error parsing EPUB {epub_path}: {e}")
        
    return spine_files, chapters_text_map

def html_unescape(text):
    import html
    return html.unescape(text)

# ----------------- CFI Generation Helper -----------------
def recursive_cfi_search(element, target_text, current_path=""):
    """
    Recursively walks XHTML element tree to locate target_text and returns the DOM path.
    Children paths are calculated as (1-based child index * 2) due to EPUB CFI specs.
    """
    children = list(element)
    for i, child in enumerate(children):
        path = f"{current_path}/{ (i + 1) * 2 }"
        text_content = "".join(child.itertext())
        if target_text in text_content:
            res = recursive_cfi_search(child, target_text, path)
            if res:
                return res
                
    if element.text and target_text in element.text:
        offset = element.text.find(target_text)
        return f"{current_path}/1:{offset}"
    return None

def generate_epub_cfi(epub_path, chapter_path, spine_idx, target_text):
    """
    Generates standard epubcfi string for the given target_text inside chapter_path.
    """
    if not os.path.exists(epub_path):
        return None
        
    # Get short sentences or keywords from target_text to find local block
    cleaned_target = target_text.strip()
    if len(cleaned_target) > 50:
        cleaned_target = cleaned_target[:50]
        
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            html_bytes = z.read(chapter_path)
            # Remove namespace declarations to simplify ET parsing
            html_str = html_bytes.decode('utf-8', errors='ignore')
            html_str = re.sub(r'\sxmlns="[^"]+"', '', html_str, count=1)
            
            root = ET.fromstring(html_str)
            body = root.find('.//body')
            if body is None:
                body = root # fallback
                
            dom_path = recursive_cfi_search(body, cleaned_target, "")
            if dom_path:
                # Drizzle uses /6/{spine_idx * 2}!/4 for body, followed by paragraph path
                # spine_idx is 0-indexed in our spine_files list, so (spine_idx + 1) * 2
                spine_part = (spine_idx + 1) * 2
                return f"epubcfi(/6/{spine_part}!/4{dom_path})"
    except Exception as e:
        logger.error(f"Error generating CFI for {chapter_path}: {e}")
    return None

# ----------------- Audio Extraction -----------------
def extract_audio_chunk(audio_path, start_sec, duration_sec, output_path):
    """
    Uses ffmpeg to slice a chunk of audio file.
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", audio_path,
        "-t", str(duration_sec),
        "-c:a", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        output_path
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception as e:
        logger.error(f"ffmpeg extraction failed: {e}")
        return False

# ----------------- Core Synchronization Logic -----------------
def sync_audiobook_to_ebook(db, user_id, source_book_id, target_book_id, audiobook_progress):
    """
    Sync progress: Audiobook ➔ Ebook
    """
    logger.info(f"Syncing Audiobook {source_book_id} to Ebook {target_book_id}")
    
    # 1. Fetch target ebook files
    with db.cursor() as cur:
        cur.execute("SELECT id, absolute_path, format FROM book_files WHERE book_id = %s AND format = 'epub';", (target_book_id,))
        ebook_file = cur.fetchone()
        if not ebook_file:
            logger.warn("Target ebook file not found.")
            return
            
        # Get source audiobook file info
        cur.execute("SELECT absolute_path, duration_seconds FROM book_files WHERE id = %s;", (audiobook_progress['current_file_id'],))
        audio_file = cur.fetchone()
        if not audio_file:
            logger.warn("Source audiobook file not found.")
            return

    host_audio_path = to_host_path(audio_file['absolute_path'])
    host_epub_path = to_host_path(ebook_file['absolute_path'])
    
    # 2. Extract 15s audio clip starting 5 seconds before current pause position
    start_sec = max(0, float(audiobook_progress['position_seconds']) - 5)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav:
        temp_wav_path = temp_wav.name
        
    try:
        if not extract_audio_chunk(host_audio_path, start_sec, 15, temp_wav_path):
            return
            
        # 3. Transcribe audio chunk
        segments, _ = model.transcribe(temp_wav_path, beam_size=5, language="en")
        transcribed_text = " ".join([seg.text for seg in segments]).strip()
        logger.info(f"Transcribed audio: '{transcribed_text}'")
        if not transcribed_text:
            logger.warn("No speech transcribed from audio clip.")
            return
            
        # 4. Parse EPUB text and search for match
        spine_files, chapters_text_map = parse_epub_spine_and_text(host_epub_path)
        if not chapters_text_map:
            logger.warn("Failed to parse target EPUB text.")
            return
            
        # Use fuzzy search to find which chapter matches the transcribed text best
        chapter_paths = list(chapters_text_map.keys())
        chapter_texts = [chapters_text_map[path] for path in chapter_paths]
        
        # Search for matched chapter
        match_res = process.extractOne(transcribed_text, chapter_texts, scorer=fuzz.partial_ratio)
        if not match_res or match_res[1] < 70:
            logger.warn(f"No good chapter match found in EPUB. Best score: {match_res[1] if match_res else 0}")
            return
            
        matched_text_block = match_res[0]
        matched_chapter_path = chapter_paths[chapter_texts.index(matched_text_block)]
        spine_idx = [x[1] for x in spine_files].index(matched_chapter_path)
        
        # 5. Find exact text position within chapter text using rolling window
        # Get matching substring within the chapter text
        words = transcribed_text.split()
        window_size = len(words)
        chapter_words = matched_text_block.split()
        
        best_ratio = 0
        best_start = 0
        for i in range(len(chapter_words) - window_size + 1):
            sub_str = " ".join(chapter_words[i:i+window_size])
            ratio = fuzz.ratio(transcribed_text, sub_str)
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i
                
        matched_sentence_in_epub = " ".join(chapter_words[best_start:best_start+window_size])
        logger.info(f"Matched sentence in EPUB chapter: '{matched_sentence_in_epub}'")
        
        # Calculate percentage in the EPUB
        # Estimate overall percentage by combining previous chapters' lengths + matched offset
        total_len = sum(len(txt) for txt in chapter_texts)
        preceding_len = sum(len(chapters_text_map[sf[1]]) for sf in spine_files[:spine_idx])
        matched_char_offset = matched_text_block.find(matched_sentence_in_epub)
        if matched_char_offset == -1:
            matched_char_offset = 0
            
        abs_char_pos = preceding_len + matched_char_offset
        percentage = min(100.0, max(0.0, (abs_char_pos / total_len) * 100.0))
        
        # Generate CFI
        cfi = generate_epub_cfi(host_epub_path, matched_chapter_path, spine_idx, matched_sentence_in_epub)
        
        # 6. Update target Ebook progress in database
        logger.info(f"Writing synced Ebook progress: percentage={percentage:.2f}%, cfi={cfi}")
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reading_progress (book_file_id, user_id, percentage, cfi, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (book_file_id, user_id) DO UPDATE
                SET percentage = EXCLUDED.percentage, cfi = EXCLUDED.cfi, updated_at = NOW();
                """,
                (ebook_file['id'], user_id, percentage, cfi)
            )
            db.commit()
            
    finally:
        if os.path.exists(temp_wav_path):
            os.remove(temp_wav_path)

def sync_ebook_to_audiobook(db, user_id, source_book_id, target_book_id, ebook_progress):
    """
    Sync progress: Ebook ➔ Audiobook
    """
    logger.info(f"Syncing Ebook {source_book_id} to Audiobook {target_book_id}")
    
    # 1. Fetch ebook file and matching audiobook files
    with db.cursor() as cur:
        cur.execute("SELECT id, absolute_path FROM book_files WHERE book_id = %s AND format = 'epub';", (source_book_id,))
        ebook_file = cur.fetchone()
        if not ebook_file:
            logger.warn("Source ebook file not found.")
            return
            
        cur.execute("SELECT id, absolute_path, duration_seconds, sort_order FROM book_files WHERE book_id = %s AND format IN ('m4b', 'mp3', 'm4a', 'aac') ORDER BY sort_order ASC;", (target_book_id,))
        audio_files = cur.fetchall()
        if not audio_files:
            logger.warn("Target audiobook files not found.")
            return

    host_epub_path = to_host_path(ebook_file['absolute_path'])
    
    # 2. Parse EPUB texts
    spine_files, chapters_text_map = parse_epub_spine_and_text(host_epub_path)
    if not chapters_text_map:
        logger.warn("Failed to parse EPUB texts.")
        return
        
    # Get current sentence to search
    # Find matching character offset from percentage
    total_len = sum(len(txt) for txt in chapters_text_map.values())
    target_char_pos = int((ebook_progress['percentage'] / 100.0) * total_len)
    
    # Locate the text paragraph around target_char_pos
    curr_len = 0
    target_sentence = ""
    for _, chapter_path in spine_files:
        txt = chapters_text_map[chapter_path]
        if curr_len + len(txt) >= target_char_pos:
            offset = target_char_pos - curr_len
            words_list = txt.split()
            accumulated = 0
            word_idx = 0
            for idx, w in enumerate(words_list):
                accumulated += len(w) + 1
                if accumulated >= offset:
                    word_idx = idx
                    break
            start_word = max(0, word_idx - 3)
            end_word = min(len(words_list), word_idx + 12)
            target_sentence = " ".join(words_list[start_word:end_word]).strip()
            break
        curr_len += len(txt)
        
    if not target_sentence:
        logger.warn("Could not extract target reading text sentence.")
        return
        
    logger.info(f"Target sentence to search in audio: '{target_sentence}'")
    
    # 3. Locate the target audio file and position window based on percentage
    total_duration = sum(f['duration_seconds'] for f in audio_files)
    target_audio_time = (ebook_progress['percentage'] / 100.0) * total_duration
    
    curr_duration = 0
    matched_audio_file = None
    relative_time_offset = 0
    for f in audio_files:
        if curr_duration + f['duration_seconds'] >= target_audio_time:
            matched_audio_file = f
            relative_time_offset = target_audio_time - curr_duration
            break
        curr_duration += f['duration_seconds']
        
    if not matched_audio_file:
        matched_audio_file = audio_files[-1]
        relative_time_offset = matched_audio_file['duration_seconds']
        
    host_audio_path = to_host_path(matched_audio_file['absolute_path'])
    
    # We do a retry loop with wider window if narrow match fails
    for window_name, w_offset, w_dur in [("narrow", 150, 300), ("wide", 500, 1000)]:
        start_sec = max(0, relative_time_offset - w_offset)
        duration_sec = min(w_dur, float(matched_audio_file['duration_seconds']) - start_sec)
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav:
            temp_wav_path = temp_wav.name
            
        try:
            if not extract_audio_chunk(host_audio_path, start_sec, duration_sec, temp_wav_path):
                continue
                
            # 5. Transcribe window and search for matching text timestamps
            segments, _ = model.transcribe(temp_wav_path, beam_size=5, word_timestamps=True)
            
            # Flatten all words with their start/end timestamps
            all_words = []
            for segment in segments:
                for word in segment.words:
                    all_words.append({
                        'word': re.sub(r'[^\w\s]', '', word.word.lower().strip()),
                        'start': word.start,
                        'end': word.end
                    })
                    
            if not all_words:
                continue
                
            # Fuzzy match target sentence inside transcription words
            target_words = [re.sub(r'[^\w\s]', '', w.lower().strip()) for w in target_sentence.split() if w.strip()]
            target_len = len(target_words)
            
            best_ratio = 0
            best_idx = 0
            for i in range(len(all_words) - target_len + 1):
                sub_words = [w['word'] for w in all_words[i:i+target_len]]
                ratio = fuzz.ratio(" ".join(target_words), " ".join(sub_words))
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = i
                    
            if best_ratio >= 70:
                matched_word_info = all_words[best_idx]
                offset_in_slice = matched_word_info['start']
                
                # Final absolute playback time
                final_playback_seconds = float(start_sec + offset_in_slice)
                final_percentage = float(min(100.0, max(0.0, ((curr_duration + final_playback_seconds) / total_duration) * 100.0)))
                
                logger.info(f"Writing synced Audiobook progress: percentage={final_percentage:.2f}%, file_id={matched_audio_file['id']}, position_seconds={final_playback_seconds:.1f}s")
                
                # 6. Update target Audiobook progress in database
                with db.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO audiobook_progress (user_id, book_id, percentage, current_file_id, position_seconds, updated_at)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (user_id, book_id) DO UPDATE
                        SET percentage = EXCLUDED.percentage, current_file_id = EXCLUDED.current_file_id, position_seconds = EXCLUDED.position_seconds, updated_at = NOW();
                        """,
                        (user_id, target_book_id, final_percentage, matched_audio_file['id'], final_playback_seconds)
                    )
                    db.commit()
                return
            else:
                logger.warning(f"Could not locate matching sentence in {window_name} window. Best score: {best_ratio}%")
        finally:
            if os.path.exists(temp_wav_path):
                os.remove(temp_wav_path)

# ----------------- Daemon Loop -----------------
def main():
    logger.info("Voxmark daemon started.")
    
    # Store the last processed timestamp
    from datetime import timezone
    last_poll_time = datetime.now(timezone.utc)
    
    # Track active reading/listening sessions for debouncing
    # Key: book_id -> { 'type': 'audio'|'ebook', 'last_activity': datetime, 'percentage': float, 'data': dict }
    active_sessions = {}
    
    while True:
        db = None
        try:
            db = get_db_conn()
            
            # 1. Check for recent audiobook progress updates
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, book_id, percentage, current_file_id, position_seconds, updated_at 
                    FROM audiobook_progress 
                    WHERE updated_at > %s;
                    """,
                    (last_poll_time,)
                )
                ab_rows = cur.fetchall()
                for row in ab_rows:
                    active_sessions[row['book_id']] = {
                        'type': 'audio',
                        'last_activity': row['updated_at'],
                        'user_id': row['user_id'],
                        'percentage': row['percentage'],
                        'data': row
                    }
                    
            # 2. Check for recent ebook progress updates
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT rp.book_file_id, rp.user_id, rp.percentage, rp.cfi, rp.page_number, rp.position_seconds, rp.updated_at, bf.book_id
                    FROM reading_progress rp
                    JOIN book_files bf ON rp.book_file_id = bf.id
                    WHERE rp.updated_at > %s;
                    """,
                    (last_poll_time,)
                )
                eb_rows = cur.fetchall()
                for row in eb_rows:
                    active_sessions[row['book_id']] = {
                        'type': 'ebook',
                        'last_activity': row['updated_at'],
                        'user_id': row['user_id'],
                        'percentage': row['percentage'],
                        'data': row
                    }
                    
            # Update last_poll_time to the current database time
            last_poll_time = datetime.now(timezone.utc)
            
            # 3. Process sessions that have been idle/debounced for >= DEBOUNCE_SECONDS
            now = datetime.now(timezone.utc)
            processed_books = []
            
            for book_id, session in active_sessions.items():
                idle_sec = (now - session['last_activity']).total_seconds()
                if idle_sec >= DEBOUNCE_SECONDS:
                    processed_books.append(book_id)
                    
                    # Search for matching alternate format of the same book
                    with db.cursor() as cur:
                        # Get title
                        cur.execute("SELECT title FROM book_metadata WHERE book_id = %s;", (book_id,))
                        meta = cur.fetchone()
                        if not meta:
                            continue
                        title = meta['title']
                        
                        # Get authors
                        cur.execute(
                            """
                            SELECT a.name 
                            FROM book_authors ba
                            JOIN authors a ON ba.author_id = a.id
                            WHERE ba.book_id = %s;
                            """,
                            (book_id,)
                        )
                        authors = [r['name'] for r in cur.fetchall()]
                        if not authors:
                            continue
                            
                        # Fetch all other books by the same authors
                        cur.execute(
                            """
                            SELECT DISTINCT b.id, bm.title
                            FROM books b
                            JOIN book_metadata bm ON b.id = bm.book_id
                            JOIN book_authors ba ON b.id = ba.book_id
                            JOIN authors a ON ba.author_id = a.id
                            WHERE a.name = ANY(%s) AND b.id != %s;
                            """,
                            (authors, book_id)
                        )
                        candidates = cur.fetchall()
                        
                        def normalize_title(t):
                            if not t: return ""
                            t = re.sub(r'\[[^\]]+\]', '', t)
                            t = re.sub(r'\([^\)]+\)', '', t)
                            t = re.sub(r'[:\-–—]', ' ', t)
                            return re.sub(r'\s+', ' ', t).strip().lower()
                        
                        norm_source = normalize_title(title)
                        matches = [c for c in candidates if normalize_title(c['title']) == norm_source]
                        
                    # Filter matching formats
                    for match in matches:
                        match_book_id = match['id']
                        if session['type'] == 'audio':
                            # Source is audiobook, target should be ebook
                            sync_audiobook_to_ebook(db, session['user_id'], book_id, match_book_id, session['data'])
                        elif session['type'] == 'ebook':
                            # Source is ebook, target should be audiobook
                            sync_ebook_to_audiobook(db, session['user_id'], book_id, match_book_id, session['data'])
                            
            # Clear processed sessions
            for b_id in processed_books:
                del active_sessions[b_id]
                
        except Exception as e:
            logger.error(f"Error in daemon loop: {e}")
        finally:
            if db:
                db.close()
                
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
