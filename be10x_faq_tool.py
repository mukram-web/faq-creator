#!/usr/bin/env python3
"""
be10x_faq_tool.py
=================
Turn one Be10X "AI Career Accelerator" session into two deliverables:
  1. <Session>_chat.txt   - the Zoom chat, extracted from the drive-download zip (if one exists)
  2. <Session>_FAQ.docx   - a styled "Top 15 FAQs with Answers" document

WHAT IT AUTOMATES (deterministic, no model needed):
  - locate the session's folder inside the Zoom export zip/dir (fuzzy match, handles the "#U2014" em-dash mangling)
  - extract the chat file (meeting_saved_new_chat.txt / meeting_saved_chat.txt) if present
  - mine learner questions from the chat (excludes the instructor, Team Be10x, Fireflies bot)
  - read the session transcript (.vtt / .txt / .md / .docx) or a notes handout
  - render the exact house-style .docx (navy/accent/grey, Arial, US Letter, page-numbered footer)
  - validate structure

WHAT NEEDS A MODEL (the actual FAQ writing):
  - generate 15 faithful, learner-facing Q&A pairs from the material.
    Default engine = Anthropic Claude API (set ANTHROPIC_API_KEY). A --dry-run mode fills
    placeholder Q&As so you can test the whole pipeline without a key or any cost.

CLI:
  python be10x_faq_tool.py TRANSCRIPT [--zip drive.zip | --dir extracted/] [--out OUT]
         [--session "10x Productivity at Work with Claude"] [--instructor "Rohit"]
         [--provider anthropic] [--model claude-sonnet-4-6] [--n 15] [--dry-run]

Requires: python-docx  (and 'anthropic' unless --dry-run / provider=none)
"""

import argparse, json, os, re, shutil, sys, tempfile, zipfile
from pathlib import Path

# ------------------------------------------------------------------ styling
NAVY, ACCENT, GREY, RULE_GREY = "1F3A5F", "2E75B6", "6B7280", "D9D9D9"
EYEBROW = "BE10X  \u2022  AI CAREER ACCELERATOR"
SUBTITLE = "Top 15 Frequently Asked Questions, with Answers"
FOOTER_LEFT = "Be10X \u2022 AI Career Accelerator"

# ------------------------------------------------------------------ helpers: slug / text

def slugify(title: str) -> str:
    """'Build, Position & Get Your First Clients' -> 'Build_Position_and_Get_Your_First_Clients'"""
    t = title.replace("&", " and ")
    t = re.sub(r"[^0-9A-Za-z]+", " ", t).strip()
    return re.sub(r"\s+", "_", t)


def read_transcript(path: Path) -> str:
    """Read a .vtt / .txt / .md / .docx into clean plain text."""
    suf = path.suffix.lower()
    if suf == ".docx":
        from docx import Document as _D
        return "\n".join(p.text for p in _D(str(path)).paragraphs if p.text.strip())
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if suf == ".vtt":
        out = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or s == "WEBVTT" or s.isdigit():
                continue
            if "-->" in s:                       # timestamp cue line
                continue
            if re.match(r"^NOTE\b", s):
                continue
            out.append(s)
        return "\n".join(out)
    return raw


# ------------------------------------------------------------------ folder match + chat

def _norm(s: str) -> str:
    s = s.replace("#U2014", " ").replace("_", " ")
    return re.sub(r"[^0-9a-z]+", " ", s.lower()).strip()


def find_session_folder(root: Path, hint: str):
    """Best-effort fuzzy match of a per-session folder by title tokens. Returns Path or None."""
    hint_tokens = set(_norm(hint).split())
    stop = {"ai", "cap", "b", "the", "of", "a", "for", "and", "with", "your",
            "using", "part", "e", "ecap", "bsi", "at", "on", "to"}
    key = hint_tokens - stop
    best, best_score = None, 0.0
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        toks = set(_norm(d.name).split())
        overlap = len(key & toks)
        score = overlap / max(1, len(key)) if key else 0
        # small bonus for raw substring of a distinctive chunk
        if _norm(hint) and _norm(hint) in _norm(d.name):
            score += 0.5
        if score > best_score:
            best, best_score = d, score
    return best if best_score >= 0.34 else None


def extract_chat(folder: Path, out_dir: Path, slug: str):
    """Copy the Zoom chat file to <out>/<slug>_chat.txt. Returns path or None."""
    if folder is None:
        return None
    for name in ("meeting_saved_new_chat.txt", "meeting_saved_chat.txt"):
        hits = list(folder.glob(name)) + list(folder.glob(f"*{name}"))
        if hits:
            dest = out_dir / f"{slug}_chat.txt"
            shutil.copyfile(hits[0], dest)
            return dest
    return None


_HDR = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}:\d{2}\s+From\s+(.+?)\s+to\s+(.+?)(?:\s+\(direct message\))?:\s*$"
)
_QWORDS = ("what", "why", "how", "when", "where", "which", "who", "can", "does",
           "do", "is", "are", "should", "will", "could", "would", "if", "any")


def mine_questions(chat_path: Path, instructor_names=None, limit=400):
    """Return de-duped learner questions from a Zoom chat export."""
    if not chat_path or not Path(chat_path).exists():
        return []
    exclude = {"team be10x", "team be10x team", "fireflies.ai notetaker", "fireflies", "fireflies notetaker"}
    for nm in (instructor_names or []):
        if nm:
            exclude.add(nm.strip().lower())
    text = Path(chat_path).read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    msgs, i = [], 0
    while i < len(lines):
        m = _HDR.match(lines[i].strip())
        if not m:
            i += 1
            continue
        sender = m.group(1).strip()
        i += 1
        body = []
        while i < len(lines) and not _HDR.match(lines[i].strip()):
            if lines[i].strip():
                body.append(lines[i].strip())
            i += 1
        if sender.lower() in exclude:
            continue
        text_body = " ".join(body).strip()
        if not text_body:
            continue
        low = text_body.lower()
        if "fireflies" in low or "realtime notes here" in low:  # drop the notetaker-bot join blurb
            continue
        if len(text_body) > 300:                     # skip pasted conversation blobs
            continue
        if text_body.count("?") >= 3:                # skip multi-question dumps
            continue
        if re.search(r"\b\d{1,2}:\d{2}\b", text_body):  # skip pasted logs with timestamps
            continue
        if "?" in text_body or low.split()[0] in _QWORDS:
            if "?" in text_body:                     # keep just the question part
                text_body = text_body[:text_body.index("?") + 1].strip()
            msgs.append(text_body)
    seen, out = set(), []
    for q in msgs:
        k = re.sub(r"[^a-z0-9 ]", "", q.lower()).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out[:limit]


# ------------------------------------------------------------------ source note

def build_source_note(has_chat: bool, input_is_notes: bool, extractive: bool = False) -> str:
    material = "materials" if input_is_notes else "transcript"
    if has_chat:
        note = f"Compiled from learner questions in the live session chat and the session {material}."
    elif input_is_notes:
        note = ("Compiled from the session materials (no separate chat export was available for "
                "this session; the questions below cover the key concepts taught in the session).")
    else:
        note = ("Compiled from the session recording / transcript (no separate chat export was available "
                "for this session; the questions below are the ones learners raised live during the session).")
    if extractive:
        note += " Answers were auto-drafted from the material and should be reviewed before publishing."
    return note


# ------------------------------------------------------------------ docx builder

def _oxml(tag):
    from docx.oxml import OxmlElement
    return OxmlElement(tag)

def _qn(tag):
    from docx.oxml.ns import qn
    return qn(tag)

def _set_border(paragraph, edge, color, sz, space=1):
    pPr = paragraph._p.get_or_add_pPr()
    pbdr = pPr.find(_qn("w:pBdr"))
    if pbdr is None:
        pbdr = _oxml("w:pBdr"); pPr.append(pbdr)
    el = _oxml("w:" + edge)
    el.set(_qn("w:val"), "single"); el.set(_qn("w:sz"), str(sz))
    el.set(_qn("w:space"), str(space)); el.set(_qn("w:color"), color)
    pbdr.append(el)

def _char_spacing(run, val=30):
    rPr = run._r.get_or_add_rPr()
    sp = _oxml("w:spacing"); sp.set(_qn("w:val"), str(val)); rPr.append(sp)

def _field(paragraph, instr, color=GREY, size_pt=8):
    from docx.shared import Pt, RGBColor
    run = paragraph.add_run()
    run.font.name = "Arial"; run.font.size = Pt(size_pt); run.font.color.rgb = RGBColor.from_string(color)
    r = run._r
    b = _oxml("w:fldChar"); b.set(_qn("w:fldCharType"), "begin"); r.append(b)
    it = _oxml("w:instrText"); it.set(_qn("xml:space"), "preserve"); it.text = instr; r.append(it)
    e = _oxml("w:fldChar"); e.set(_qn("w:fldCharType"), "end"); r.append(e)

def _run(p, text, *, size, color=None, bold=False, italic=False):
    from docx.shared import Pt, RGBColor
    r = p.add_run(text)
    r.font.name = "Arial"; r.font.size = Pt(size); r.bold = bold; r.italic = italic
    if color:
        r.font.color.rgb = RGBColor.from_string(color)
    return r


def build_docx(session: dict, out_path: Path):
    """session = {title, source, faqs:[(q,a), ... 15]}"""
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_TAB_ALIGNMENT

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"; normal.font.size = Pt(11)

    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.top_margin = sec.bottom_margin = sec.left_margin = sec.right_margin = Inches(1)

    # eyebrow
    p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(2)
    _char_spacing(_run(p, EYEBROW, size=9, color=ACCENT, bold=True), 30)
    # title
    p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(2)
    _run(p, session["title"], size=18, color=NAVY, bold=True)
    # subtitle
    p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(5)
    _run(p, SUBTITLE, size=12, color=GREY, italic=True)
    # accent rule
    p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(8)
    _run(p, "", size=6); _set_border(p, "bottom", ACCENT, 12, 1)
    # source note
    p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(11)
    _run(p, session["source"], size=10, color=GREY, italic=True)

    # Q&A
    for q, a in session["faqs"]:
        h = doc.add_paragraph()
        h.paragraph_format.keep_with_next = True
        h.paragraph_format.space_before = Pt(11); h.paragraph_format.space_after = Pt(3)
        _run(h, q, size=12, color=NAVY, bold=True)
        b = doc.add_paragraph()
        b.paragraph_format.space_after = Pt(3); b.paragraph_format.line_spacing = 1.15
        _run(b, a, size=11)

    # footer: left text + right "Page X of Y", top border
    fp = sec.footer.paragraphs[0]
    fp.text = ""
    _set_border(fp, "top", RULE_GREY, 4, 6)
    fp.paragraph_format.tab_stops.add_tab_stop(Inches(6.5), WD_TAB_ALIGNMENT.RIGHT)
    _run(fp, FOOTER_LEFT, size=8, color=GREY)
    _run(fp, "\t", size=8)
    _run(fp, "Page ", size=8, color=GREY)
    _field(fp, "PAGE"); _run(fp, " of ", size=8, color=GREY); _field(fp, "NUMPAGES")

    doc.save(str(out_path))
    return out_path


# ------------------------------------------------------------------ the model step (FAQ writing)

SYSTEM_PROMPT = """You are an expert curriculum editor for Be10X, an Indian EdTech company running the "AI Career Accelerator" cohorts. You read ONE training session's material and produce a "Top 15 FAQs with Answers".

Rules:
- Output EXACTLY 15 question-answer pairs.
- Questions are what a learner who attended THIS session would actually ask, in plain learner voice. If a live chat is provided, prefer the questions learners actually raised; otherwise cover the key concepts taught. Order them to follow the session's arc.
- Answers are PROSE and SHORT: 1-2 sentences, NEVER more than 35 words total, so each answer fits in two printed lines. NO bullet points, NO headers, NO markdown. Write for a working professional. Name the specific tool/number/step from the material, but cut everything non-essential.
- Ground every answer ONLY in the provided material. Never invent tools, prices, features or steps that are not supported.
- Accuracy over loyalty: if the instructor stated something factually wrong, do NOT repeat it as fact - restate it accurately and plainly (e.g. "100% secure", "zero hallucination", "100% accurate" -> the realistic version). Keep honest instructor caveats intact.
- No meta-commentary, no "the instructor said", no timestamps, no citations.

Return STRICT JSON ONLY (no code fences), exactly:
{"title": "<session title in Title Case, no trailing punctuation>",
 "faqs": [{"q": "...", "a": "..."}, ... exactly 15 objects]}"""


def _build_user_message(transcript_text, mined_questions, has_chat, input_is_notes, session_hint):
    kind = "a clean session-notes handout" if input_is_notes else "a spoken session transcript"
    parts = [
        f"Session (hint): {session_hint or '(infer from the material)'}",
        f"Input type: {kind}.",
        f"Live chat provided: {'yes' if has_chat else 'no'}.",
    ]
    if mined_questions:
        joined = "\n".join(f"- {q}" for q in mined_questions[:200])
        parts.append("\nLearner questions mined from the live chat (use these to shape the FAQ; "
                     "dedupe, tidy grammar, and ground answers in the material):\n" + joined)
    parts.append("\n=== SESSION MATERIAL START ===\n" + transcript_text.strip() + "\n=== SESSION MATERIAL END ===")
    parts.append("\nProduce the 15 Q&A now as strict JSON.")
    return "\n".join(parts)


def _placeholder_faqs(n=15):
    return [(f"Placeholder question {i} - run without --dry-run to generate real FAQs.",
             "This is placeholder answer text used to test the pipeline end to end without calling a model. "
             "Set ANTHROPIC_API_KEY and drop --dry-run to produce real, session-grounded answers here.")
            for i in range(1, n + 1)]


_STOP = set("a an the of to in on for and or is are was were be been being with as at by from "
            "this that these those it its i you we they he she do does did can could will would "
            "should how what why when where which who whom your our their my me us not no yes if "
            "about into over under than then so such but also just very more most only own same "
            "here there all any each other some how's what's".split())


def _sentences(text):
    text = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 25]


def _content_tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP and len(w) > 2}


def _clean_q(q):
    q = q.strip()
    if "?" in q:
        q = q[:q.index("?") + 1]
    q = re.sub(r"\s+", " ", q).strip()
    if q and not q.endswith("?"):
        q += "?"
    return (q[0].upper() + q[1:]) if q else q


def _heading_questions(transcript_text, n):
    heads = []
    for line in transcript_text.splitlines():
        s = line.strip()
        m = re.match(r"^\d+(?:\.\d+)*\s+(.{3,80})$", s)
        if m:
            h = re.sub(r"[.:]+$", "", m.group(1)).strip()
            if h and h.lower() not in {x.lower() for x in heads}:
                heads.append(h)
    return [f"What does the session cover on {h.lower()}?" for h in heads[:n]]


def _best_answer(qtok, sents, sent_toks):
    scored = sorted(((len(qtok & tk), idx) for idx, (tk, _s) in enumerate(sent_toks)),
                    key=lambda x: -x[0])
    keep = sorted(i for score, i in scored[:4] if score > 0)
    top = scored[0][0] if scored else 0
    if not keep:
        return "", 0
    ans = " ".join(sents[i] for i in keep[:5])
    if not ans.endswith((".", "!", "?")):
        ans += "."
    return ans, top


def _generate_extractive(transcript_text, mined_questions, n):
    """No-model FAQ: only ask questions the material can answer; answer with matching sentences."""
    sents = _sentences(transcript_text)
    sent_toks = [(_content_tokens(s), s) for s in sents]

    cands, seen = [], set()
    for q in mined_questions:
        cq = _clean_q(q)
        k = re.sub(r"[^a-z0-9 ]", "", cq.lower()).strip()
        if 12 <= len(cq) <= 160 and k and k not in seen:
            seen.add(k); cands.append(cq)

    scored_q = []
    for cq in cands:
        ans, top = _best_answer(_content_tokens(cq), sents, sent_toks)
        if ans and top >= 1:                         # keep only answerable questions
            scored_q.append((top, cq, ans))
    scored_q.sort(key=lambda x: -x[0])

    faqs, covered = [], set()
    for top, cq, ans in scored_q:
        toks = _content_tokens(cq)
        if not faqs or (toks - covered):             # favour variety of topics
            faqs.append((cq, ans)); covered |= toks
        if len(faqs) >= n:
            break

    if len(faqs) < n:                                # fall back to heading topics
        for hq in _heading_questions(transcript_text, n * 3):
            ans, _ = _best_answer(_content_tokens(hq), sents, sent_toks)
            if ans:
                faqs.append((hq, ans))
            if len(faqs) >= n:
                break
    while len(faqs) < n:
        faqs.append(("Additional question - review and complete.",
                     "Answer to be completed from the session material."))
    return faqs[:n]


_DEFAULT_MODEL = {"anthropic": "claude-sonnet-4-6", "gemini": "gemini-2.5-flash", "ollama": "llama3.1"}


def generate_faqs(transcript_text, mined_questions, has_chat, input_is_notes,
                  session_hint, provider="ollama", model=None,
                  api_key=None, n=15, dry_run=False):
    """Return (title, faqs[list of (q,a)])."""
    if dry_run or provider == "none":
        return (session_hint or "Session"), _placeholder_faqs(n)

    if provider == "extractive":                        # <-- no model at all
        return (session_hint or "Session"), _generate_extractive(transcript_text, mined_questions, n)

    model = model or _DEFAULT_MODEL.get(provider)
    user_msg = _build_user_message(transcript_text, mined_questions, has_chat, input_is_notes, session_hint)

    if provider == "ollama":                            # <-- free, local, private (recommended)
        import urllib.request
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        payload = {"model": model, "stream": False, "format": "json",
                   "options": {"temperature": 0.3, "num_ctx": 8192},
                   "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": user_msg}]}
        req = urllib.request.Request(host + "/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            raw = json.loads(r.read().decode())["message"]["content"]
    elif provider == "gemini":                          # <-- free cloud tier
        import time
        from google import genai
        client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY"))
        # The free tier routinely throws transient 503 (overloaded) / 429 (rate limit):
        # retry with backoff, then fall back to the other Gemini model before giving up.
        _FALLBACK = {"gemini-2.5-flash": "gemini-2.5-pro", "gemini-2.5-pro": "gemini-2.5-flash"}
        models_to_try = [model] + ([_FALLBACK[model]] if model in _FALLBACK else [])
        raw, last_err = None, None
        for m in models_to_try:
            for attempt in range(3):
                try:
                    r = client.models.generate_content(
                        model=m, contents=SYSTEM_PROMPT + "\n\n" + user_msg,
                        config={"response_mime_type": "application/json"})
                    raw = r.text
                    break
                except Exception as e:
                    last_err = e
                    transient = any(t in str(e) for t in
                                    ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "overloaded"))
                    if not transient:
                        raise
                    if attempt < 2:
                        time.sleep(2 ** (attempt + 1))  # 2, 4 s
            if raw is not None:
                break
            print(f"[warn] {m} unavailable, trying fallback model", file=sys.stderr)
        if raw is None:
            raise last_err
    elif provider == "anthropic":                       # <-- paid
        import anthropic
        client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        resp = client.messages.create(model=model, max_tokens=8000, system=SYSTEM_PROMPT,
                                       messages=[{"role": "user", "content": user_msg}])
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    else:
        raise ValueError(f"unknown provider: {provider}")

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).rstrip("`").rstrip()
    data = json.loads(raw)
    faqs = [(f["q"].strip(), f["a"].strip()) for f in data["faqs"]]
    if len(faqs) != n:
        print(f"[warn] model returned {len(faqs)} FAQs (expected {n})", file=sys.stderr)
    return data.get("title", session_hint or "Session").strip(), faqs


# ------------------------------------------------------------------ orchestrator

def run(transcript, zip_path=None, extracted_dir=None, out_dir="output",
        session=None, instructor=None, provider="ollama",
        model=None, n=15, dry_run=False, api_key=None):
    transcript = Path(transcript)
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    input_is_notes = transcript.suffix.lower() in (".md", ".txt", ".docx")  # heuristic; overridable in future

    # resolve the extracted zoom-export directory
    root = None
    tmp = None
    if extracted_dir:
        root = Path(extracted_dir)
    elif zip_path:
        tmp = Path(tempfile.mkdtemp(prefix="zoomzip_"))
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        # if the zip has a single top folder, descend into it
        kids = [p for p in tmp.iterdir() if p.is_dir()]
        root = kids[0] if len(kids) == 1 and not any(p.is_file() for p in tmp.iterdir()) else tmp

    hint = session or transcript.stem.replace("_", " ")
    folder = find_session_folder(root, hint) if root else None
    slug = slugify(hint)

    chat_path = extract_chat(folder, out_dir, slug) if folder else None
    has_chat = chat_path is not None
    questions = mine_questions(chat_path, [instructor] if instructor else None) if has_chat else []

    transcript_text = read_transcript(transcript)
    title, faqs = generate_faqs(transcript_text, questions, has_chat, input_is_notes,
                                hint, provider=provider, model=model, api_key=api_key,
                                n=n, dry_run=dry_run)
    slug = slugify(title)  # re-slug from the model's clean title
    if chat_path and slugify(hint) != slug:                     # keep chat filename aligned to title
        new_chat = out_dir / f"{slug}_chat.txt"
        shutil.move(str(chat_path), new_chat); chat_path = new_chat

    faq_path = out_dir / f"{slug}_FAQ.docx"
    build_docx({"title": title,
                "source": build_source_note(has_chat, input_is_notes, provider == "extractive"),
                "faqs": faqs}, faq_path)

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    return {"title": title, "matched_folder": folder.name if folder else None,
            "chat": str(chat_path) if chat_path else None,
            "mined_questions": len(questions), "faq": str(faq_path)}


def main():
    ap = argparse.ArgumentParser(description="Be10X session -> chat + Top-15 FAQ .docx")
    ap.add_argument("transcript", help="session transcript (.vtt/.txt/.md/.docx) or notes handout")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--zip", help="path to the Zoom drive-download .zip")
    src.add_argument("--dir", help="path to an already-extracted Zoom export folder")
    ap.add_argument("--out", default="output", help="output directory (default: ./output)")
    ap.add_argument("--session", help='session title/hint, e.g. "10x Productivity at Work with Claude"')
    ap.add_argument("--instructor", help="instructor name to exclude from mined questions")
    ap.add_argument("--provider", default="ollama",
                    choices=["ollama", "gemini", "anthropic", "extractive", "none"],
                    help="ollama=free local model (default); gemini=free cloud; "
                         "anthropic=paid; extractive=no model at all")
    ap.add_argument("--model", default=None,
                    help="model name (default per provider: ollama->llama3.1, "
                         "gemini->gemini-2.5-flash, anthropic->claude-sonnet-4-6)")
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--dry-run", action="store_true", help="skip the model; use placeholder FAQs")
    a = ap.parse_args()

    res = run(a.transcript, zip_path=a.zip, extracted_dir=a.dir, out_dir=a.out,
              session=a.session, instructor=a.instructor, provider=a.provider,
              model=a.model, n=a.n, dry_run=a.dry_run)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
