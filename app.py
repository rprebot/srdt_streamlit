"""SRDT — Testeur (version Streamlit)."""
from __future__ import annotations

import html
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import gspread
import requests
import streamlit as st
from google.oauth2.service_account import Credentials

ENDPOINT = "/api/generate"
SEARCH_ENDPOINT = "/api/v1/search"
FEEDBACK_FILE = Path(__file__).parent / "feedback.jsonl"
ENV = "preprod"
SOURCE_URL_PREFIX = "https://www.legifrance.gouv.fr/conv_coll"
SOURCE_TYPE_CONV_COLL = "conv_coll"
SOURCE_TYPE_JURISPRUDENCE = "judilibre"
JURISPRUDENCE_TOP_K = 10
JURISPRUDENCE_TOP_N = 3
SHEET_HEADERS = ["receivedAt", "question", "agreementId", "sourceType", "url", "score", "adapted"]

st.set_page_config(page_title="SRDT — Testeur", page_icon="⚖️", layout="wide")

st.markdown(
    """
    <style>
      div[data-testid="stAppViewContainer"] { background: #f5f5f5; }
      .block-container { max-width: 1200px; margin: 0 auto; }
      .srdt-header {
        background: #1e3a5f; color: #fff; padding: 14px 20px;
        border-radius: 8px; margin-bottom: 20px;
      }
      .srdt-header h1 { font-size: 1.15rem; margin: 0; }
      .srdt-header span { font-size: 0.85rem; opacity: 0.7; }
      .badge {
        display: inline-block; font-size: 0.7rem; padding: 2px 10px;
        border-radius: 99px; font-weight: 600; margin-left: 8px;
      }
      .badge-preprod { background: #fff3cd; color: #856404; }
      .query-recap {
        background: #eef2ff; border-left: 4px solid #3730a3; padding: 10px 14px;
        border-radius: 6px; margin-bottom: 16px; font-size: 0.88rem; color: #1f2937;
      }
      .query-recap b { color: #3730a3; }
      .source-card-head {
        display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;
      }
      .source-index { font-weight: 700; font-size: 0.8rem; color: #374151; }
      .source-status {
        font-size: 0.68rem; font-weight: 700; padding: 2px 9px; border-radius: 99px; white-space: nowrap;
      }
      .source-status-pending { background: #f3f4f6; color: #6b7280; }
      .source-status-ok { background: #d4edda; color: #155724; }
      .source-status-ko { background: #f8d7da; color: #721c24; }
      .source-link {
        font-size: 0.85rem; word-break: break-all; line-height: 1.4;
      }
      .source-label {
        font-size: 0.78rem; color: #374151; margin-bottom: 4px; line-height: 1.3;
      }
      .source-feedback-hint {
        font-size: 0.75rem; color: #6b7280; margin: 8px 0 2px;
      }
    </style>
    <div class="srdt-header">
      <h1>SRDT — Testeur &nbsp;<span>Service de Renseignement en Droit du Travail</span></h1>
    </div>
    """,
    unsafe_allow_html=True,
)


def get_env_config(env: str) -> dict:
    return {
        "url": st.secrets[f"URL_{env.upper()}"].rstrip("/"),
        "token": st.secrets[f"TOKEN_{env.upper()}"],
    }


def call_api(question: str, agreement_id: str | None) -> dict:
    cfg = get_env_config(ENV)
    payload = {"question": question}
    if agreement_id:
        payload["agreementId"] = agreement_id
    resp = requests.post(
        cfg["url"] + ENDPOINT,
        json=payload,
        headers={"Authorization": f"Bearer {cfg['token']}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def get_search_config(env: str) -> dict:
    return {
        "url": st.secrets[f"SEARCH_URL_{env.upper()}"].rstrip("/"),
        "token": st.secrets[f"SEARCH_TOKEN_{env.upper()}"],
    }


def call_jurisprudence_api(question: str) -> tuple[list[dict], dict]:
    cfg = get_search_config(ENV)
    payload = {
        "prompts": [question],
        "options": {"collections": ["judilibre"], "hybrid": True, "top_K": JURISPRUDENCE_TOP_K},
    }
    resp = requests.post(
        cfg["url"] + SEARCH_ENDPOINT,
        json=payload,
        headers={"Authorization": f"Bearer {cfg['token']}"},
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    chunks = body.get("top_chunks", [])
    return sorted(chunks, key=lambda c: c.get("score") or 0, reverse=True)[:JURISPRUDENCE_TOP_N], body


@st.cache_resource
def get_worksheet():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(st.secrets["SHEET_ID"]).sheet1
    if sheet.row_values(1) != SHEET_HEADERS:
        sheet.update(range_name="A1", values=[SHEET_HEADERS])
    return sheet


def save_feedback(entry: dict) -> None:
    record = {"receivedAt": datetime.now(timezone.utc).isoformat(), **entry}
    with FEEDBACK_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        ws = get_worksheet()
        ws.append_row(
            [str(record.get(k, "")) for k in SHEET_HEADERS],
            value_input_option="USER_ENTERED",
        )
    except Exception as e:
        st.toast(f"⚠️ Écriture Google Sheet impossible : {e}", icon="⚠️")


def render_source_list(
    items: list[tuple[str, float | None, str | None]],
    *,
    key_prefix: str,
    source_type: str,
    title: str,
    empty_caption: str,
    ctx: dict,
    query_id: str,
    feedback_state: dict,
) -> None:
    # Synchronise feedback_state avec les clics de ce run avant tout affichage,
    # sinon badges et barre de progression restent un cran en retard sur le clic.
    for i, (url, score, _label) in enumerate(items):
        fb_key = f"{key_prefix}_{query_id}_{i}"
        choice = st.session_state.get(fb_key)
        if choice is not None:
            adapted = choice == 1
            if feedback_state.get(fb_key) != adapted:
                feedback_state[fb_key] = adapted
                save_feedback({**ctx, "sourceType": source_type, "url": url, "score": score, "adapted": adapted})

    st.markdown(f"#### {title} ({len(items)})")

    if not items:
        st.caption(empty_caption)
    else:
        fb_keys = [f"{key_prefix}_{query_id}_{i}" for i in range(len(items))]
        answered = sum(1 for k in fb_keys if k in feedback_state)
        st.progress(answered / len(items), text=f"{answered}/{len(items)} sources notées")

    for i, (url, score, label) in enumerate(items):
        safe_url = html.escape(url, quote=True)
        fb_key = f"{key_prefix}_{query_id}_{i}"
        status = feedback_state.get(fb_key)
        if status is None:
            status_html = "<span class='source-status source-status-pending'>À noter</span>"
        elif status:
            status_html = "<span class='source-status source-status-ok'>👍 Pertinente</span>"
        else:
            status_html = "<span class='source-status source-status-ko'>👎 Non pertinente</span>"

        with st.container(border=True):
            label_html = f"<div class='source-label'>{html.escape(label)}</div>" if label else ""
            st.markdown(
                f"<div class='source-card-head'>"
                f"<span class='source-index'>Source {i + 1}</span>{status_html}"
                f"</div>"
                f"{label_html}"
                f"<a class='source-link' href='{safe_url}' target='_blank'>↗ {safe_url}</a>",
                unsafe_allow_html=True,
            )
            st.markdown("<div class='source-feedback-hint'>Cette source est-elle pertinente ?</div>", unsafe_allow_html=True)
            st.feedback("thumbs", key=fb_key)


# --- Formulaire ---
with st.form("query_form"):
    question = st.text_area(
        "Question", placeholder="Posez votre question en droit du travail…", height=120, key="question_input"
    )
    agreement_id = st.text_input("Convention collective (IDCC)", placeholder="ex: 2120", key="agreement_input")
    submitted = st.form_submit_button("Envoyer", use_container_width=True)

if submitted:
    if not question.strip():
        st.error("Merci de saisir une question.")
    else:
        with st.spinner("Génération de la réponse…"):
            try:
                t0 = time.time()
                result = call_api(question.strip(), agreement_id.strip() or None)
                result["_elapsed"] = time.time() - t0
            except requests.HTTPError as e:
                result = {"success": False, "error": f"{e.response.status_code} {e.response.reason} — {e.response.text}"}
            except requests.RequestException as e:
                result = {"success": False, "error": str(e)}

            jurisprudence: list[dict] = []
            jurisprudence_debug: dict = {}
            if result.get("success"):
                try:
                    jurisprudence, jurisprudence_debug = call_jurisprudence_api(question.strip())
                except requests.RequestException as e:
                    st.toast(f"⚠️ Recherche de jurisprudence impossible : {e}", icon="⚠️")
                    jurisprudence_debug = {"error": str(e)}

        st.session_state["result"] = result
        st.session_state["jurisprudence"] = jurisprudence
        st.session_state["jurisprudence_debug"] = jurisprudence_debug
        st.session_state["query_id"] = uuid.uuid4().hex
        st.session_state["query_ctx"] = {
            "question": question.strip(),
            "agreementId": agreement_id.strip() or None,
        }

# --- Nouvelle conversation ---
if st.session_state.get("result") is not None:
    if st.button("🔄 Nouvelle conversation"):
        for k in list(st.session_state.keys()):
            if k in ("result", "jurisprudence", "jurisprudence_debug", "query_id", "query_ctx", "feedback_given", "question_input", "agreement_input") or k.startswith("fb_") or k.startswith("fbj_"):
                del st.session_state[k]
        st.rerun()

# --- Résultats ---
result = st.session_state.get("result")
if result is not None:
    if not result.get("success"):
        st.error(f"Erreur : {result.get('error', 'réponse invalide')}")
    else:
        data = result["data"]
        ctx = st.session_state["query_ctx"]
        query_id = st.session_state["query_id"]
        gen = data.get("generated", {})

        # Sources uniques (score conservé uniquement pour le tri, non affiché)
        chunks = data.get("localSearchChunks", [])
        by_url: dict[str, float | None] = {}
        for c in chunks:
            url = c.get("metadata", {}).get("url")
            if not url or not url.startswith(SOURCE_URL_PREFIX):
                continue
            score = c.get("score")
            if url not in by_url or (score is not None and (by_url[url] is None or score > by_url[url])):
                by_url[url] = score
        sources = sorted(by_url.items(), key=lambda kv: (kv[1] if kv[1] is not None else 0), reverse=True)
        source_items = [(url, score, None) for url, score in sources]

        # Jurisprudence (top 3 décisions Judilibre les mieux notées, cf. call_jurisprudence_api)
        jurisprudence_items: list[tuple[str, float | None, str | None]] = []
        for c in st.session_state.get("jurisprudence", []):
            meta = c.get("metadata", {})
            url = meta.get("url")
            if not url:
                continue
            label = meta.get("title")
            if label and len(label) > 140:
                label = label[:137] + "…"
            jurisprudence_items.append((url, c.get("score"), label))

        feedback_state = st.session_state.setdefault("feedback_given", {})

        col_main, col_sources, col_juris = st.columns([2, 1, 1], gap="large")

        with col_main:
            st.markdown(
                "### Réponse <span class='badge badge-preprod'>PREPROD</span>",
                unsafe_allow_html=True,
            )
            recap = f"<b>Question :</b> {html.escape(ctx['question'])}"
            if ctx.get("agreementId"):
                recap += f"<br><b>Convention collective :</b> {html.escape(ctx['agreementId'])}"
            st.markdown(f"<div class='query-recap'>{recap}</div>", unsafe_allow_html=True)

            st.markdown(gen.get("text") or "_Aucune réponse_")

            with st.expander("Voir le prompt système"):
                st.text(data.get("debug", {}).get("systemPrompt", "—"))

        with col_sources:
            render_source_list(
                source_items,
                key_prefix="fb",
                source_type=SOURCE_TYPE_CONV_COLL,
                title="Sources",
                empty_caption="Aucune source Légifrance trouvée.",
                ctx=ctx,
                query_id=query_id,
                feedback_state=feedback_state,
            )

        with col_juris:
            render_source_list(
                jurisprudence_items,
                key_prefix="fbj",
                source_type=SOURCE_TYPE_JURISPRUDENCE,
                title="Jurisprudence",
                empty_caption="Aucune décision de jurisprudence trouvée.",
                ctx=ctx,
                query_id=query_id,
                feedback_state=feedback_state,
            )
            with st.expander("Voir la réponse brute de recherche"):
                st.json(st.session_state.get("jurisprudence_debug", {}))
