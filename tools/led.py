from gpiozero import LED
from time import sleep
from datetime import datetime
from random import random
import argparse
import os


#sleep(15)
print("Starting led.py")

parse = argparse.ArgumentParser(description='LED file logger')
parse.add_argument('--file', '-f', default='led_log.txt', help='Log file name')

full_dir = os.path.dirname(os.path.realpath(__file__))

args = parse.parse_args()

led = LED(16)

fon = open(f'{full_dir}/{args.file}', "w")
try:
	for i in range(100):
		rand = random()
		led.on()
		current_time = datetime.now()
		fon.write(f'{current_time}\n')
		print(f"{i}: LED On")
		sleep(1 + rand)
		rand = random()
		led.off()
		print(f"{i}: LED Off")
		sleep(1 + rand)
except KeyboardInterrupt:
	print("Exiting program")
finally:
	led.off()
	fon.close()
