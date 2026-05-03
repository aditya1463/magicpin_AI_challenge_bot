FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# The Anthropic API key must be injected at runtime:
# docker run -e ANTHROPIC_API_KEY=sk-ant-... -p 8080:8080 vera-bot
EXPOSE 8080
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8080"]
