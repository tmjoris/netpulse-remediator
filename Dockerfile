FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY netpulse ./netpulse
COPY examples ./examples
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["netpulse", "serve", "--host", "0.0.0.0", "--port", "8000"]
