"""
app.py — Be10X "Session → FAQ" (Gemini + Google Drive).

Flow:
  1. Upload a session transcript (.vtt / .txt / .md / .docx)
  2. The session name is auto-filled from the filename (editable)
  3. The Gemini key comes from secrets (or is pasted, if no `gemini_api_key` secret is set)
  4. Click Generate — the app finds that session's folder in the "Weekly Sessions Files"
     Shared Drive (service account), pulls its Zoom chat if one exists, and Gemini writes
     the Top-15 FAQ. Download the styled .docx.

Secrets:
  - service_account_json  -> .streamlit/secrets.toml (local) or the Streamlit Cloud Secrets dashboard.
  - gemini_api_key        -> same place (optional). When set, the app uses it and hides the key
    field; when absent, the key is typed into the app each run (not stored).

    pip install -r requirements.txt
    streamlit run app.py
"""
import re
import tempfile
from pathlib import Path

import streamlit as st

import be10x_faq_tool as tool
import drive_client as dc

st.set_page_config(page_title="Be10X · Session → FAQ", page_icon="📝", layout="centered")
st.title("Be10X · Session → FAQ")
st.caption("Upload a session transcript, pick up its chat from Drive, and get the Top-15 FAQ .docx.")


def derive_hint(filename: str) -> str:
    """Best-effort session name from a transcript filename."""
    stem = Path(filename).stem.replace("_", " ")
    stem = re.sub(r"\b(transcript|trans|vtt|recording|zoom)\b", "", stem, flags=re.I)
    return re.sub(r"\s+", " ", stem).strip()


def get_service():
    """Build the Drive service from the service-account JSON in Streamlit secrets."""
    if "service_account_json" not in st.secrets:
        st.error("Missing `service_account_json` in secrets. Add it to .streamlit/secrets.toml "
                 "(local) or the Streamlit Cloud Secrets dashboard.")
        st.stop()
    return dc.build_service(st.secrets["service_account_json"])


def run_generation(transcript_bytes, transcript_name, hint, api_key, model, folder_id=None):
    """Match Drive → (chat) → Gemini → .docx. Returns a result dict for rendering."""
    workdir = Path(tempfile.mkdtemp(prefix="be10x_"))
    tpath = workdir / transcript_name
    tpath.write_bytes(transcript_bytes)
    out_dir = workdir / "out"
    out_dir.mkdir(exist_ok=True)

    svc = get_service()
    slug = tool.slugify(hint)

    with st.spinner("Finding the session in Drive…"):
        drv = dc.fetch_session_chat(svc, hint, out_dir, slug, folder_id=folder_id)

    questions = []
    if drv["has_chat"]:
        questions = tool.mine_questions(Path(drv["chat_path"]))

    with st.spinner(f"Writing the FAQ with {model}…"):
        title, faqs = tool.generate_faqs(
            tool.read_transcript(tpath), questions,
            has_chat=drv["has_chat"], input_is_notes=False,
            session_hint=hint, provider="gemini", model=model, api_key=api_key, n=15,
        )

    slug = tool.slugify(title)
    faq_path = out_dir / f"{slug}_FAQ.docx"
    tool.build_docx({"title": title,
                     "source": tool.build_source_note(drv["has_chat"], False, False),
                     "faqs": faqs}, faq_path)

    return {"title": title, "faqs": faqs, "faq_path": str(faq_path),
            "matched_folder": drv["matched_folder"], "has_chat": drv["has_chat"],
            "n_questions": len(questions), "candidates": drv["candidates"]}


# ---------------------------------------------------------------- inputs
transcript = st.file_uploader("Session transcript", type=["vtt", "txt", "md", "docx"])

if transcript is not None and st.session_state.get("_last_file") != transcript.name:
    st.session_state["session_name"] = derive_hint(transcript.name)
    st.session_state["_last_file"] = transcript.name

session = st.text_input("Session name", key="session_name",
                        placeholder="Build your First Enterprise-grade AI Chatbot Application",
                        help="Auto-filled from the filename. Used to find the session's folder in Drive.")

saved_key = st.secrets.get("gemini_api_key", "").strip()
if saved_key:
    api_key = saved_key
    model = st.selectbox("Model", ["gemini-2.5-flash", "gemini-2.5-pro"], index=0)
    st.caption("Using the Gemini API key from secrets.")
else:
    col1, col2 = st.columns([2, 1])
    api_key = col1.text_input("Gemini API key", type="password",
                              help="Add `gemini_api_key` to secrets to skip this. "
                                   "Get one at aistudio.google.com/apikey")
    model = col2.selectbox("Model", ["gemini-2.5-flash", "gemini-2.5-pro"], index=0)

ready = transcript is not None and bool(session.strip()) and bool(api_key.strip())
if st.button("Generate FAQ", type="primary", disabled=not ready):
    try:
        res = run_generation(transcript.getvalue(), transcript.name, session.strip(), api_key.strip(), model)
        st.session_state["result"] = res
        st.session_state["_tx_bytes"] = transcript.getvalue()
        st.session_state["_tx_name"] = transcript.name
    except Exception as e:
        st.error(f"{type(e).__name__}: {e}")
        st.stop()

# ---------------------------------------------------------------- results
res = st.session_state.get("result")
if res:
    st.success(f"**{res['title']}**")
    c1, c2, c3 = st.columns(3)
    c1.metric("Chat found", "yes" if res["has_chat"] else "no")
    c2.metric("Questions mined", res["n_questions"])
    c3.metric("FAQs", len(res["faqs"]))
    st.caption(f"Matched Drive folder: **{res['matched_folder']}**")

    faq_bytes = Path(res["faq_path"]).read_bytes()
    st.download_button("⬇ Download FAQ (.docx)", faq_bytes, file_name=Path(res["faq_path"]).name,
                       mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    # Safety net for auto-pick: let the user override the matched folder if titles collided.
    others = [c for c in res["candidates"] if c["name"] != res["matched_folder"]]
    if others:
        with st.expander("Matched the wrong session? Pick the folder manually"):
            labels = {f"{c['name']}  (score {c['score']})": c["id"] for c in res["candidates"]}
            choice = st.selectbox("Session folder", list(labels.keys()))
            if st.button("Regenerate with this folder"):
                try:
                    res2 = run_generation(st.session_state["_tx_bytes"], st.session_state["_tx_name"],
                                          session.strip(), api_key.strip(), model,
                                          folder_id=labels[choice])
                    st.session_state["result"] = res2
                    st.rerun()
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")

    with st.expander("Preview the 15 Q&A"):
        for i, (q, a) in enumerate(res["faqs"], 1):
            st.markdown(f"**Q{i}. {q}**")
            st.write(a)
