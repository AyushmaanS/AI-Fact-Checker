import streamlit as st

import api_client

SPINNER_TEXT = "Researching claims — this can take up to 90 seconds..."

LABEL_COLORS = {
    "TRUE": "#22863a",
    "FALSE": "#d73a49",
    "PARTIALLY_TRUE": "#b08800",
    "MISLEADING": "#e36209",
    "UNVERIFIABLE": "#6a737d",
    "OUTDATED": "#005cc5",
    "SATIRE": "#6f42c1",
}


def render_badge(label: str) -> None:
    color = LABEL_COLORS.get(label, "#6a737d")
    st.markdown(
        f'<span style="background-color:{color}; color:white; padding:4px 14px; '
        f'border-radius:12px; font-weight:600; font-size:0.9rem;">{label.replace("_", " ")}</span>',
        unsafe_allow_html=True,
    )


def render_evidence_column(evidence_lines: list[dict], stance: str, header: str) -> None:
    st.markdown(f"**{header}**")
    matching = [e for e in evidence_lines if e["stance"] == stance]
    if not matching:
        st.caption("None found.")
        return
    for e in matching:
        st.markdown(f"- {e['paraphrase']} ([source]({e['citation']}))")


def render_verdict(claim: dict, verdict: dict) -> None:
    st.markdown(f"**Claim:** {claim['text']}")
    render_badge(verdict["label"])
    st.markdown(verdict["summary_line"])

    confidence = verdict["confidence_score"]
    st.progress(min(max(confidence, 0.0), 1.0))
    st.caption(f"Confidence: {confidence * 100:.0f}%")

    with st.expander("View evidence"):
        col1, col2 = st.columns(2)
        with col1:
            render_evidence_column(verdict["evidence_lines"], "for", "Supporting evidence")
        with col2:
            render_evidence_column(verdict["evidence_lines"], "against", "Contradicting evidence")

    st.divider()


def render_response(response: dict) -> None:
    claims = response.get("claims") or []

    if not claims:
        st.info(response.get("message") or "No verifiable claims were found.")
        return

    if len(claims) > 1 and response.get("aggregate_label"):
        st.markdown("### Overall verdict")
        render_badge(response["aggregate_label"])
        st.divider()

    verdicts_by_claim_id = {v["claim_id"]: v for v in response.get("verdicts") or []}
    for claim in claims:
        verdict = verdicts_by_claim_id.get(claim["claim_id"])
        if verdict:
            render_verdict(claim, verdict)


def run_check(tab_key: str, api_call) -> None:
    """Calls the API, stashing the result/error in session_state under
    tab_key so it survives the rerun that happens when Streamlit re-executes
    the whole script on the next interaction (e.g. switching tabs)."""
    with st.spinner(SPINNER_TEXT):
        try:
            response = api_call()
        except api_client.ApiError as exc:
            st.session_state.errors[tab_key] = str(exc)
            st.session_state.results.pop(tab_key, None)
        else:
            st.session_state.results[tab_key] = response
            st.session_state.errors.pop(tab_key, None)


def render_tab_result(tab_key: str) -> None:
    error = st.session_state.errors.get(tab_key)
    if error:
        st.error(error)
        return
    response = st.session_state.results.get(tab_key)
    if response:
        render_response(response)


st.set_page_config(page_title="AI Fact-Checker")
st.session_state.setdefault("results", {})
st.session_state.setdefault("errors", {})

st.title("AI Fact-Checker")

tab_text, tab_url, tab_upload = st.tabs(["Paste a claim", "Paste a URL", "Upload a video"])

with tab_text:
    claim_text = st.text_area("Claim or passage to check", height=150, key="claim_text")
    if st.button("Check claim", key="submit_text"):
        if not claim_text.strip():
            st.warning("Enter some text first.")
        else:
            run_check("text", lambda: api_client.verify_text(claim_text))
    render_tab_result("text")

with tab_url:
    claim_url = st.text_input("Instagram Reel or post URL", key="claim_url")
    if st.button("Check URL", key="submit_url"):
        if not claim_url.strip():
            st.warning("Enter a URL first.")
        else:
            run_check("url", lambda: api_client.verify_url(claim_url))
    render_tab_result("url")

with tab_upload:
    uploaded_file = st.file_uploader(
        "Video file", type=["mp4", "mov", "avi", "mkv", "webm"], key="claim_upload"
    )
    if st.button("Check video", key="submit_upload"):
        if uploaded_file is None:
            st.warning("Choose a file first.")
        else:
            run_check("upload", lambda: api_client.verify_upload(uploaded_file))
    render_tab_result("upload")
