import os

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

st.set_page_config(page_title="Meta AI Hackathon - Document Analyzer", layout="wide")
st.title("Meta AI Hackathon - Document Analyzer")

api_key = os.getenv("BASETEN_API_KEY")

if not api_key:
    st.error(
        "Missing BASETEN_API_KEY. Add it to your environment or a `.env` file, then restart the app."
    )
    st.stop()

client = OpenAI(
    api_key=api_key,
    base_url="https://inference.baseten.co/v1",
)

user_text = st.text_area(
    "Paste text to analyze",
    height=300,
    placeholder="Paste a document, article, or notes here...",
)

if st.button("Summarize & Analyze", type="primary"):
    if not user_text.strip():
        st.warning("Please paste some text before analyzing.")
    else:
        with st.spinner("Analyzing with Meta Muse Spark..."):
            try:
                response = client.chat.completions.create(
                    model="meta-models/muse-spark-open-1026",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a helpful document analyst. Summarize the user's text "
                                "clearly and concisely, then list exactly 3 key takeaways."
                            ),
                        },
                        {"role": "user", "content": user_text},
                    ],
                )
                result = response.choices[0].message.content
                st.subheader("Analysis")
                st.markdown(result)
            except Exception as e:
                st.error(f"The analysis request failed: {e}")
