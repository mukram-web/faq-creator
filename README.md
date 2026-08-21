# Be10X Session → FAQ Tool

Turn one AI Career Accelerator session into two files, automatically:

1. `<Session>_chat.txt` — the Zoom chat, pulled out of the drive-download zip (only when one exists)
2. `<Session>_FAQ.docx` — the styled **Top 15 FAQs with Answers** in the usual Be10X house style

No more running each session through Claude by hand.

---

## What runs without any paid API

The tool automates all the mechanical work with **no model and no cost**: matching the session's
folder inside the Zoom export, extracting the chat, mining the real learner questions, building the
exact styled `.docx`, and validating it.

The only step that benefits from a model is **writing** the 15 answers. You have three free choices
plus one paid one:

| `--provider`  | Cost        | Runs where     | Answer quality | Notes |
|---------------|-------------|----------------|----------------|-------|
| `ollama`      | **Free**    | Your machine   | Good           | **Recommended.** Private, offline, no limits. |
| `extractive`  | **Free**    | Your machine   | Draft-grade    | **No model at all.** Stitches transcript sentences + ranks real chat questions. Review before publishing. |
| `gemini`      | Free tier   | Google cloud   | Very good      | Free API key, but data leaves your machine and the free tier is rate-limited. |
| `anthropic`   | Paid        | Anthropic cloud| Best           | Optional. Only if you ever want it. |

---

## Install

```bash
pip install -r requirements.txt          # installs python-docx

# For the recommended free engine (Ollama):
#   1. install the app:  https://ollama.com
#   2. pull a model:     ollama pull llama3.1      (or llama3, qwen2.5, mistral — whatever you pulled in the "Run open-source models" session)
```

`ollama` and `extractive` need nothing else. `gemini` needs `pip install google-genai`; `anthropic` needs `pip install anthropic`.

---

## Run it

**Free local model (recommended):**
```bash
python be10x_faq_tool.py transcript.vtt \
  --zip drive-download.zip \
  --session "10x Productivity at Work with Claude" \
  --instructor "Rohit" \
  --provider ollama --model llama3.1 \
  --out ./output
```

**No model at all (zero dependencies beyond python-docx):**
```bash
python be10x_faq_tool.py transcript.vtt --zip drive-download.zip \
  --session "10x Productivity at Work with Claude" --provider extractive --out ./output
```

**Free cloud (Gemini):**
```bash
export GEMINI_API_KEY=...        # from https://aistudio.google.com/apikey
python be10x_faq_tool.py transcript.vtt --zip drive-download.zip \
  --session "..." --provider gemini --out ./output
```

**Test the whole pipeline with no model and no key** (placeholder answers):
```bash
python be10x_faq_tool.py transcript.vtt --dir ./already-extracted --dry-run --out ./output
```

### Inputs
- **transcript**: the session `.vtt` (Zoom/Fireflies), or a notes handout as `.txt` / `.md` / `.docx`.
  (The zip never contains the spoken transcript — that comes from the recording. The zip has the *chat*.)
- **`--zip`** the drive-download `.zip`, **or** **`--dir`** a folder you already unzipped.
- **`--session`** the title/hint used to find the right folder and name the files. If omitted, the
  transcript's filename is used.
- **`--instructor`** name to exclude from mined questions (so the instructor's own chat lines aren't
  treated as learner questions).

### Options
`--provider` ollama | extractive | gemini | anthropic | none · `--model` (defaults per provider:
`llama3.1` / `gemini-2.5-flash` / `claude-sonnet-4-6`) · `--n` number of FAQs (default 15) ·
`--out` output folder (default `./output`) · `--dry-run` skip the model.

Environment: `OLLAMA_HOST` (default `http://localhost:11434`), `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`.

---

## Do a whole batch at once

```bash
for vtt in transcripts/*.vtt; do
  python be10x_faq_tool.py "$vtt" --zip drive-download.zip --provider ollama --out ./output
done
```

---

## Quality note (please read)

- **ollama / gemini** produce real, learner-facing prose answers, grounded only in the material, and
  quietly correct obviously wrong instructor claims — the same rules used for the hand-made docs.
- **extractive** is a *draft*: it can only reuse sentences already in the transcript and rank the real
  chat questions. The document says so on its face. Use it when you can't run a model, then edit.
- Long transcripts may exceed a local model's context window (`num_ctx` is set to 8192). If Ollama
  starts truncating, use a longer-context model or split the transcript. (Automatic chunking is a
  future improvement.)

---

## The app (Streamlit · Google Drive + Gemini)

`app.py` is the main front-end. It pulls the session's Zoom chat straight from the **"Weekly Sessions
Files" Shared Drive** (via a Google service account) — no zip upload — and uses **Gemini** to write the
FAQ. Output is the **FAQ `.docx`** only.

**Flow:** upload a transcript (`.vtt`/`.txt`/`.md`/`.docx`) → the session name auto-fills from the
filename → paste your Gemini API key → **Generate**. The app auto-picks the best-matching Drive folder
(with a "pick manually" override if a title collides across dates), pulls its chat if one exists, and
Gemini writes the Top-15 FAQ. Download the styled `.docx`.

```bash
pip install -r requirements.txt
streamlit run app.py
```

### Secrets

Both the Gemini key and the Drive **service account** are read from secrets — put them in
`.streamlit/secrets.toml` locally, and in the **Streamlit Cloud → Secrets** dashboard when deployed:

```toml
# .streamlit/secrets.toml   (gitignored — never commit)
gemini_api_key = "your-gemini-key"       # optional — with it set, the app never asks for the key

service_account_json = '''
{ ...the full service-account key JSON... }
'''
```

If `gemini_api_key` is missing, the app falls back to asking for the key each run.

Prereqs (one-time): the service account must be a **member of the Shared Drive**, and the **Google Drive
API** must be enabled in its Cloud project. `ollama` can't run on a hosted box, which is fine — the app
is Gemini-only. (The CLI in `be10x_faq_tool.py` still supports `--zip` + ollama/extractive/anthropic for
local batch use.)

---

## House style (baked in, don't need to touch)

US Letter, 1" margins, Arial. Navy `#1F3A5F` titles, accent `#2E75B6` rules and `Q#.` labels, grey
`#6B7280` subtitle/source/footer. Eyebrow → title → subtitle → accent rule → source note → 15× (bold
`Q#.` heading + prose `A.`), page-numbered footer. 35 paragraphs, validates clean.
