FROM python:3.11-slim

# Don't buffer logs — otherwise crash output can be lost before it reaches the
# host's log viewer, which makes deploy failures much harder to diagnose.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user: if the app is ever compromised, the attacker lands as
# an unprivileged account rather than root inside the container.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Shell form so ${PORT} is expanded by the shell at runtime — Railway injects it.
# Two workers, which is safe now that jobs live in the database instead of
# process memory (a poll can land on either worker and still find the job).
CMD sh -c "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 2 --timeout 120"
