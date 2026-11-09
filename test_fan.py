import RPi.GPIO as GPIO
import time

FAN_GPIO = 16   # BCM GPIO number

GPIO.setmode(GPIO.BCM)
GPIO.setup(FAN_GPIO, GPIO.OUT)

print("Fan ON")
GPIO.output(FAN_GPIO, GPIO.HIGH)
time.sleep(5)

print("Fan OFF")
GPIO.output(FAN_GPIO, GPIO.LOW)

GPIO.cleanup()
