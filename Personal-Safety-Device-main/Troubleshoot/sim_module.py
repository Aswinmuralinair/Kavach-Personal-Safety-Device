import serial
import time

PORT = "/dev/ttyUSB2"   # same as config['serial_port']
BAUD = 115200

def send(cmd, wait=2):
    ser.write((cmd + "\r\n").encode())
    time.sleep(wait)
    return ser.read_all().decode(errors="ignore")

try:
    ser = serial.Serial(PORT, BAUD, timeout=1)
    print("Serial port opened")

    print("AT Test:")
    print(send("AT"))

    print("SIM card check:")
    print(send("AT+CPIN?"))      # READY = SIM inserted

    print("Network registration:")
    print(send("AT+CREG?"))      # ,1 or ,5 = registered

    print("Signal strength:")
    print(send("AT+CSQ"))        # >10 is usable

except Exception as e:
    print("SIM7600 NOT detected:", e)
finally:
    try:
        ser.close()
    except Exception:
        pass
