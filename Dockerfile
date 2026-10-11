# Python 3.11 স্লিম ইমেজ ব্যবহার করা হচ্ছে
FROM python:3.11-slim

# এনভায়রনমেন্ট ভেরিয়েবল সেট করা
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# সিস্টেম ডিপেন্ডেন্সি ইনস্টল করা — libvips42 সহ
RUN apt-get update && apt-get install -y --no-install-recommends \
    libvips42 \
    libvips-dev \
    libheif-examples \
    libheif-dev \
    libjpeg-dev \
    libpng-dev \
    libwebp-dev \
    libtiff-dev \
    libgif-dev \
    build-essential \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# ওয়ার্কিং ডিরেক্টরি সেট করা
WORKDIR /app

# রিকোয়ারমেন্টস ফাইল কপি করে ইনস্টল করা (ক্যাশের জন্য)
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# প্রোজেক্টের বাকি ফাইল কপি করা
COPY . .

# ডিরেক্টরি তৈরি করা
RUN mkdir -p downloads converted

# অ্যাপ চালু করা
CMD ["python", "bot.py"]
