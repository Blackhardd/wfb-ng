#!/usr/bin/env python3
import socket
import time
import json
import random

GS_HOST = "groundraspberry.local"  # айпи гс
GS_PORT = 14890
SEND_INTERVAL = 1.0
COUNT = 10  # количество повторений
RSSI = -32 # RSSI: 0 = рандом каждый раз; иначе подставлять это значение во все отправки


def data_random():
    rss_random = random.randint(-50, -30)
    per_random = random.randint(0, 5)
    snr_random = random.randint(5, 10)
    return rss_random, per_random, snr_random

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    n = 0
    try:
        while True:
            if COUNT and n >= COUNT:
                break
            if RSSI == 0: # если RSSI = 0 то генерю хрень случайную 
                rssi, per, snr = data_random()
            else:
                rssi = RSSI # если RSSI не 0 то подставляю значение из переменной RSSI
                _, per, snr = data_random() # генерирую хрень случайную для PER и SNR
            payload = {
                "type": "heartbeat",
                "timestamp": time.time(),
                "status": "connected",
                "channel": "5800",
                "score": 1.0,
                "local": {"rssi": rssi, "per": per, "snr": snr},
                "remote": {"rssi": rssi, "per": per, "snr": snr},
            }
            s.sendto(json.dumps(payload).encode(), (GS_HOST, GS_PORT))
            n += 1
            print("sent", n)
            time.sleep(SEND_INTERVAL)
    finally:
        s.close()

if __name__ == "__main__":
    main()