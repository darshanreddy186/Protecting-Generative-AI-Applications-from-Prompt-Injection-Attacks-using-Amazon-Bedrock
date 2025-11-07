import os
import json
import time
import csv
import re
import boto3
import requests
import streamlit as st
from datetime import datetime

# === CONFIG ===
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "YOUR_PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "YOUR_PUSHOVER_API_TOKEN")

# Use DeepSeek as default model for ap-south-1
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "deepseek.v3-v1:0")
REGION = os.getenv("AWS_REGION", "ap-south-1")

# simple pricing assumption — adjust as needed
INPUT_COST = 0.25 / 1_000_000
OUTPUT_COST = 0.25 / 1_000_000
LOG_FILE = "refiner_logs.csv"

# === SECURITY ===
INJECTION_PATTERNS = [
    r"forget\s+(all\s+)?(prior|previous)?\s*instructions?",
    r"ignore\s+(all\s+)?(prior|previous)?\s*instructions?",
    r"\byou\s+are\s+now\b",
    r"\bsimulate\b",
    r"\bjailbreak\b",
    r"\bbypass\b",
    r"<script.?>.?</script.*?>",
    r"data:text/html",
    r"eval\(",
    r"([A-Za-z0-9+/]{200,})"
]

def is_prompt_suspicious(prompt):
    lowered = prompt.lower()
    for pattern in INJECTION_PATTERNS:
        try:
            if re.search(pattern, lowered):
                return True
        except re.error as e:
            print(f"Regex error: {e}")
    return False


# === PUSHOVER ALERTS ===
def send_suspicious_pushover(username, prompt):
    msg = f"⚠ Suspicious Prompt Blocked (UI)\nUser: {username}\nPrompt: {prompt}"
    try:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_API_TOKEN,
            "user": PUSHOVER_USER_KEY,
            "message": msg[:1024]
        })
    except Exception as e:
        print("❌ Failed to send suspicious prompt alert:", e)


def send_pushover_success(username, prompt, output, total_tokens, cost):
    msg = f"""✅ Bedrock Model Invoked (UI)
User: {username}
Prompt: {prompt}
Tokens: {total_tokens}
Cost: {cost}
Output: {output[:240]}"""
    try:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_API_TOKEN,
            "user": PUSHOVER_USER_KEY,
            "message": msg[:1024]
        })
    except Exception as e:
        print("❌ Failed to send success alert:", e)


# === BEDROCK INVOCATION ===
def invoke_bedrock(prompt, mode="refine"):
    """
    Generic Bedrock chat invocation for DeepSeek/Qwen style models.
    """

    client = boto3.client("bedrock-runtime", region_name=REGION)

    # Build message based on mode
    if mode == "refine":
        system_prompt = (
            "You are Prompt Refiner AI.\n"
            "Rewrite the user's prompt to make it clearer, more specific, and safer.\n"
            "Do NOT answer or execute it. Only rewrite it in natural English."
        )
        user_prompt = f"User prompt:\n{prompt}"

    elif mode == "grade":
        system_prompt = (
            "You are a professional evaluator.\n"
            "Rate the following user prompt on a scale of 1 to 10 for clarity, specificity, and safety.\n"
            "Respond with only a single number."
        )
        user_prompt = prompt

    else:  # suggest
        system_prompt = (
            "You are an AI assistant.\n"
            "Suggest 2–3 follow-up questions or ways to improve the user's prompt.\n"
            "Return them as short bullet points."
        )
        user_prompt = prompt

    # Chat-style body for DeepSeek/Qwen
    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 300,
        "temperature": 0.7 if mode != "grade" else 0.3
    }

    start = time.time()
    response = client.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body)
    )
    duration = time.time() - start
    result = json.loads(response["body"].read())

    # DeepSeek / Qwen response structure
    message = result.get("choices", [{}])[0].get("message", {})
    output_text = message.get("content", "").strip()

    usage = result.get("usage", {})
    total_tokens = usage.get("total_tokens", usage.get("completion_tokens", 0))
    total_cost = total_tokens * INPUT_COST  # rough estimate

    return output_text, total_tokens, f"${total_cost:.8f}", duration


# === LOGGING ===
def log_to_csv(timestamp, username, prompt, output_text, total_tokens, cost):
    with open(LOG_FILE, mode="a", newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp,
            username,
            prompt.replace("\n", " "),
            output_text.replace("\n", " "),
            total_tokens,
            cost
        ])


# === STREAMLIT UI ===
st.set_page_config(
    page_title="🧠 Prompt Refiner AI",
    page_icon="https://t1.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https://kentprotect.com/&size=256"
)

st.markdown("""
    <div style='background-color:#0f172a; padding: 1rem; border-radius: 10px; color: white; text-align: center;'>
        <h3>🧠 Prompt Refiner AI – AWS Bedrock (DeepSeek)</h3>
    </div>
""", unsafe_allow_html=True)

st.title("🧠 Prompt Refiner AI")

username = st.text_input("Username", "")
prompt = st.text_area("Enter your prompt", height=250)
submit = st.button("Submit")

if submit:
    if not username or not prompt:
        st.warning("Please enter both username and prompt.")
    elif is_prompt_suspicious(prompt):
        st.error("⚠ Suspicious prompt detected. Please revise.")
        send_suspicious_pushover(username, prompt)
    else:
        with st.spinner("⏳ Refining your prompt..."):
            try:
                refined, tokens, cost, duration = invoke_bedrock(prompt, mode="refine")
                grade, _, _, _ = invoke_bedrock(prompt, mode="grade")
                suggestions, _, _, _ = invoke_bedrock(prompt, mode="suggest")

                st.success("✅ Model Feedback:")
                st.markdown(f"*Refined Prompt:*\n\n{refined}")
                st.info(f"📊 Prompt Score: {grade}/10")
                st.markdown(f"💡 Suggested Follow-ups:\n\n{suggestions}")
                st.info(f"📊 Tokens: {tokens} | Cost: {cost} | ⏱ {duration:.2f}s")

                log_to_csv(datetime.utcnow().isoformat(), username, prompt, refined, tokens, cost)
                send_pushover_success(username, prompt, refined, tokens, cost)
            except Exception as e:
                st.error(f"❌ Error: {e}")

# === FOOTER ===
st.markdown("""
    <hr>
    <div style='text-align: center; font-size: 0.85rem;'>
        Built by <strong>KentProtect</strong> · Powered by <strong>Amazon Bedrock – DeepSeek</strong><br>
        
    </div>
""", unsafe_allow_html=True)