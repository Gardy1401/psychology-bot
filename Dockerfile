FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
# добавить корневой и выпускающий сертификаты НУЦ Минцифры
RUN curl -fsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -o /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
    && curl -fsSL https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt -o /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt \
    && update-ca-certificates
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN useradd -m botuser
USER botuser
ENV TZ=Europe/Moscow \
    TELEGRAM_TOKEN='8449687467:AAFwrSQzZhRTLpHMaVzJ5O1O3j-JQAVv--k'
CMD ["python", "-u", "app/main.py"]
