#!/usr/bin/env python3
"""
******************************************************************************
    Pi Temperature Station

    This is a Raspberry Pi project that measures weather data
    (Current: temperature, humidity, wind, and wind direction)

    Current Configuration:
    uses the RTL-SDR (Software Defined Radio) to read a
    LaCrosse W3600 series 433MHz Radio on our dock

    it uploads the data to Weather Underground
    and will save the data locall for comparison

******************************************************************************
"""
__author__ = "Mark Oliver"
__license__ = "GPL"
__maintainer__ = "MOvations"


# Save this as separete file later, before loading GitHub
# class Config:
#     # Weather Underground
#     STATION_ID = "KMIMAPLE8"
#     STATION_KEY = "UNMJ30kf"


import sys
from subprocess import PIPE, Popen, STDOUT
from threading import Thread
import time 
from secrets import STATION_ID, STATION_KEY

# import re                                             # No longer needed, but could be used to parse STDOUT
import json  # JSON was easiest with the rtl_433 framework
import datetime as dt
import pandas as pd
import numpy as np  # Data is collected and manipulated in DataFrams and Arrays
import os

from urllib.parse import urlencode  # For uploading to Weather Underground (WU)
from urllib.request import urlopen

#   Neat Trick to captuer Python2/3 imort errors
try:
    from Queue import Queue, Empty  # python 2
except ImportError:
    from queue import Queue, Empty  # python 3.x

# ============================================================================
# CMD to execute in terminal
# ============================================================================
cmd = ["/usr/local/bin/rtl_433", "-F", "json"]

#  for setting stout and queue
ON_POSIX = "posix" in sys.builtin_module_names


# ============================================================================
# CONSTANTS: WU Upload Handles
# ============================================================================

# specifies how often to measure values from the Sense HAT (in minutes)
UPLOAD_INTERVAL = 1 * 60  # Seconds
# Set to False when testing the code and/or hardware
# Set to True to enable upload of weather data to Weather Underground
WEATHER_UPLOAD = True
# the weather underground URL used to upload weather data
WU_URL = (
    "http://weatherstation.wunderground.com/weatherstation/updateweatherstation.php"
)
# some string constants
SINGLE_HASH = "#"
HASHES = "################################################"
SLASH_N = "\n"

# make sure we don't have a UPLOAD_INTERVAL > 60 min
if (UPLOAD_INTERVAL is None) or (UPLOAD_INTERVAL > 3600):
    print("The application's 'UPLOAD_INTERVAL' cannot be empty or greater than 3600")
    sys.exit(1)

print("\nInitializing Weather Underground configuration")
# wu_station_id = Config.STATION_ID
# wu_station_key = Config.STATION_KEY
wu_station_id = STATION_ID
wu_station_key = STATION_KEY

if (wu_station_id is None) or (wu_station_key is None):
    print("Missing values from the Weather Underground configuration file\n")
    sys.exit(1)

# well, made it this far, so something must have worked...
print("Successfully read Weather Underground configuration values")
print("Station ID:", wu_station_id)
# print("Station key:", wu_station_key)


# ============================================================================
# helper functions:
# ============================================================================

# use moving average to smooth readings
def get_smooth(x):
    # do we have the t object?
    if not hasattr(get_smooth, "t"):
        # then create it
        get_smooth.t = [x, x, x]
    # manage the rolling previous values
    # should be a way to do this part cleaner...
    get_smooth.t[2] = get_smooth.t[1]
    get_smooth.t[1] = get_smooth.t[0]
    get_smooth.t[0] = x
    # average the three last temperatures
    xs = (get_smooth.t[0] + get_smooth.t[1] + get_smooth.t[2]) / 3
    # print("3 temps to ave: {}, {}, {}".format(
    #     get_smooth.t[0], get_smooth.t[1], get_smooth.t[2]))
    # print("With result: {}".format(xs))
    return xs


# Climate Calculations
def rht_to_dp(temp, rh):
    """ Takes Relative Humidity & Temperature then approximates a Dew Point """
    # from https://en.wikipedia.org/wiki/Dew_point
    dp = temp - (0.36 * (100 - rh))
    # Check Calc
    # print("Temp: {} RH: {} DP: {}".format(temp, rh, dp))
    return dp


def degc_to_degf(input_temp):
    """ Convert input temp from Celcius to Fahrenheit """
    return (input_temp * 1.8) + 32


def ms_to_mph(input_speed):
    """ Convert input speed from Meters/sec to mph """
    return input_speed * 2.237


def pa_to_inches(pressure_in_pa):
    """ Convert pressure in Pascal to mmHg """
    pressure_in_inches_of_m = pressure_in_pa * 0.02953
    return pressure_in_inches_of_m


def mm_to_inches(rainfall_in_mm):
    """ Convert rainfall in millimeters to Inches """
    rainfall_in_inches = rainfall_in_mm * 0.0393701
    return rainfall_in_inches


def khm_to_mph(speed_in_kph):
    """ Convert speed in kph to MPH  """
    # for wind speed, when I find a way to measure
    speed_in_mph = speed_in_kph * 0.621371
    return speed_in_mph


def nowStr():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def streamRoll(daList):
    if len(daList) < 3:
        new_r = np.mean(daList) +  (daList[-1] - np.mean(daList)) / len(daList)
    else:
        new_r = np.mean(daList[-3:]) +  (daList[-1] - np.mean(daList[-3:])) / len(daList[-3:])
    return new_r

# ============================================================================
# Value Correcting functions:
# ============================================================================
def temp_correct(temp):
    new_temp = temp + 10.001
    return new_temp


def ws_correct(ws):
    """correct windspeed from knots """
    new_ws = ws * (1 / 0.868976)
    return new_ws


def wd_correct(wd):
    """ correct the wind direction """
    new_wd = wd - 90.0
    if new_wd < 0:
        new_wd = new_wd + 360.0
    if new_wd >= 360:
        new_wd = new_wd - 360.0


# ============================================================================
# MAIN functions:
# ============================================================================


def clearVars():
    a1 = []
    a2 = []
    a3 = []
    a4 = []
    a5 = 0
    return a1, a2, a3, a4, a5


def display_data(h, c, wd, ws):
    print(nowStr())
    print(
        "Temperature:              {:.2f} F ".format(
            degc_to_degf(temp_correct(np.mean(c)))
        )
    )
    print("Relative Humidity:        {:.2f} % ".format(np.mean(h)))
    print(
        "Wind Mean:                 {:.2f} mph".format(
            ws_correct(ms_to_mph(np.mean(ws)))
        )
    )
    print(
        "Wind Max:                  {:.2f} mph".format(
            ws_correct(ms_to_mph(np.max(ws)))
        )
    )
    print("Wind Direction:          {:.2f} deg".format(np.max(np.max(wd))))


def enqueue_output(src, out, queue):
    for line in iter(out.readline, b""):
        queue.put((src, line))
    out.close()

def upload_weather(wu_station_id, wu_station_key, tempC, humidity, windDir, windSpeed):
    """ Upload Data to Weather Underground """
    # From http://wiki.wunderground.com/index.php/PWS_-_Upload_Protocol
    # link is broken, some bindings can be found here:
    # https://www.openhab.org/addons/bindings/weatherunderground/
    print("Uploading data to Weather Underground")
    # build a weather data object
    weather_data = {
        "action": "updateraw",
        "ID": wu_station_id,
        "PASSWORD": wu_station_key,
        "dateutc": "now",
        "tempf": str(degc_to_degf(temp_correct(np.mean(tempC)))),
        "dewPtF": str(
            rht_to_dp(degc_to_degf(temp_correct(np.mean(tempC))), np.mean(humidity))
        ),
        "humidity": str(np.mean(humidity)),
        # "baromin": str(pressure),
        "winddir_avg2m": str((wd_correct(np.mean(windDir)))),
        "windspdmph_avg2m": str(ws_correct(ms_to_mph(np.mean(windSpeed)))),
        "windspeedmph": str(ws_correct(ms_to_mph(np.median(windSpeed)))),
        "winddir": str((wd_correct(np.max(windDir)))),
        "windgustmph": str(ws_correct(ms_to_mph(np.max(windSpeed)))),
        "windgustdir": str((wd_correct(np.max(windDir)))),
    }
    try:
        # there is a 'with' statement was that is better
        upload_url = WU_URL + "?" + urlencode(weather_data)
        response = urlopen(upload_url)
        html = response.read()
        print("Server response: {}\n".format(html.decode()))
        # do something
        response.close()  # best practice to close the file
    except:
        print("Exception:", sys.exc_info()[0], SLASH_N)


# ============================================================================
#  Create sub-process:
# ============================================================================

#  Note that we need to either ignore output from STDERR or
#  merge it with STDOUT due to a limitation/bug somewhere under the covers of "subprocess"
#   > this took awhile to figure out a reliable approach for handling it...
p = Popen(cmd, stdout=PIPE, stderr=STDOUT, bufsize=1, close_fds=ON_POSIX)
#   We're using a queue to capture output as it occurs
q = Queue()
t = Thread(target=enqueue_output, args=("stdout", p.stdout, q))
t.daemon = True  # thread dies with the program
t.start()

# ============================================================================
# My Headers
# ============================================================================
print(SLASH_N + HASHES)
print(SINGLE_HASH, "Pi Weather Station                          ", SINGLE_HASH)
print(SINGLE_HASH, "for LaCrosse-WS3600 read via RTL-SDR        ", SINGLE_HASH)
print(SINGLE_HASH, "By Mark H Oliver                            ", SINGLE_HASH)
print(HASHES)

print(sys.executable)
print(sys.version)

# ============================================================================
# MAIN
# ============================================================================
have_hum = False
have_temp = False
have_wd = False
have_ws = False
pulse = 0
pulse_timeout = 400000
upload_timer = 0
hum = []
tempC = []
windDir = []
windSpeed = []
roll = []
tempTrack = []

while True:
    upload_timer += 1
    # print(upload_timer )

    try:
        src, line = q.get(timeout=1)
        # if q.empty() == True:
        # raise Empty
    except Empty:
        pulse += 1
        print(pulse, end="  \n")
    else:  # got line
        pulse -= 1

        ## hacky workaround for the header files that draw errors ##
        # Goes to roughly -15 with the header files then climbs
        # to a positive value as no readings are measured
        if pulse > 0:
            # print(pulse, end="  \n")
            # Convert data to
            reading = line.decode()
            #   See if the data is something we need to act on...
            # print(reading)

            df = (
                pd.read_json(reading, lines=True)
                .set_index("time")
                .drop(["id", "model"], axis=1)
            )
            if "humidity" in df.columns:
                hum.append(df["humidity"].values)
                have_hum = True
            if "wind_direction" in df.columns:
                windDir.append(df["wind_direction"].values)
                have_wd = True
            if "wind_speed_ms" in df.columns:
                windSpeed.append(df["wind_speed_ms"].values)
                have_ws = True
            if "temperature_C" in df.columns:
                tempC = df["temperature_C"].values

                # Occationally will see temperature sprikes
                # Used this as a workaround
                tempTrack.append(tempC.flatten())
                rollingAveNum = 5
                if len(tempTrack) > rollingAveNum :
                   del tempTrack[0:(len(tempTrack)-(rollingAveNum - 1))]
                roll = streamRoll(tempTrack)

                #debugging
                print("roll:    {} \n".format(roll))
                print("tempTrack:  {} \n".format(tempTrack))

                if abs(tempTrack[-1] - roll) > 5:
                    have_temp = False
                else:
                    have_temp = True

            # print(df)
            # Check for clean data
            # if pd.isna(df["humidity"].max) or pd.isna(df["temperature_C"].max):
            #      upload_timer = 0
            # if pd.isna(df["wind_direction"].max) or pd.isna(df["wind_speed_ms"].max):
            #      upload_timer = 0

            # Set the timeout to display the
            # pulse_timeout = pulse
    finally:
        sys.stdout.flush()

    # hack to limit the amount of content displayed
    # RF timeout takes ~1 sec, roughly the same for each pulse as well
    # assumed 15 seconds to be a good buffer to allow the variables to populate
    if (
        (have_hum == True)
        and (have_temp == True)
        and (have_wd == True)
        and (have_ws == True)
    ):
        # if (pulse_timeout + 15 <= pulse):
        display_data(hum, tempC, windDir, windSpeed)

        if pulse > pulse_timeout:
            print("...   It's been a while since I've seen something  ...  Rebooting ...   ...   ...")

#            os.system("sudo apt-get update && sudo apt-get upgrade -y && sudo apt update && \
#                sudo apt dist-upgrade -y && \
#                sudo apt clean && sudo apt autoremove -y && sudo reboot")

        if upload_timer > UPLOAD_INTERVAL:
            if WEATHER_UPLOAD:
                upload_weather(
                    wu_station_id, wu_station_key, tempC[-1], hum, windDir, windSpeed
                )

                # log_weather(temp_f, t_hum, t_press, pressure, humidity,
                #             t_cpu, t_dht, h_dht, dew_pt_dht, t_tecf,
                #             dew_pt_tec, t_cort, press280_cort(p_280),
                #             a_280, cnt, ldr)
                # # upload_timer = 0
                # hum,tempC, windDir, windSpeed = clearVars()

            else:
                print("Skipping Weather Underground upload")

            upload_timer = 0
            hum, tempC, windDir, windSpeed, pulse = clearVars()


        # Reset the data trackers
        have_hum = False
        have_temp = False
        have_wd = False
        have_ws = False
