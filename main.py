import requests
import time
from pushbullet import Pushbullet
from datetime import datetime
import json

# Your Nightscout URL
NIGHTSCOUT_URL = "https://p01--larsagna--w849sqzvwqdl.code.run"

# Your Pushbullet API Key
PUSHBULLET_API_KEY = "o.ubPCxoJEw8kDk9Y9GxgeS3rEtNWTAYpO"

# Target Blood Glucose Range in mmol/L
TARGET_BG_LOW = 4.0
TARGET_BG_HIGH = 8.0

# Safety Thresholds and Limits
BOLUS_THRESHOLD = 0.2  # Minimum bolus in units
CARB_THRESHOLD = 5  # Minimum grams of carbs to recommend
MAX_BOLUS_LIMIT = 10.0  # Maximum bolus allowed in units
MAX_TEMP_BASAL_INCREASE = 0.5  # Maximum 50% basal increase
MAX_TEMP_BASAL_DECREASE = 0.5  # Maximum 50% basal decrease
GRACE_PERIOD_MINUTES = 30  # Grace period after a bolus before suggesting another

# Conversion factor for mg/dL to mmol/L (1 mmol/L = 18 mg/dL)
MGDL_TO_MMOL = 18.0

# Basal Adjustment Limits
DURATION = 60  # Duration for the temp basal in minutes

# Function to match time-based profile data
def match_profile_to_time(profile_list):
    current_time = datetime.now().time()
    current_seconds = current_time.hour * 3600 + current_time.minute * 60 + current_time.second

    matched_value = None
    for entry in profile_list:
        if entry.get('timeAsSeconds', 0) <= current_seconds:
            matched_value = entry['value']
        else:
            break

    return matched_value

# Fetch data from Nightscout
def get_nightscout_data():
    try:
        # Fetch recent glucose value
        glucose_response = requests.get(f"{NIGHTSCOUT_URL}/api/v1/entries.json?count=1")
        glucose_data = glucose_response.json()
        print("Glucose data:", glucose_data)  # Debugging output
        if isinstance(glucose_data, list) and len(glucose_data) > 0:
            glucose_data = glucose_data[0]
        else:
            print("No glucose data found.")
            return None, None, None, None, None, None, None

        # Fetch recent treatments
        treatments_response = requests.get(f"{NIGHTSCOUT_URL}/api/v1/treatments.json?count=1")
        treatment_data = treatments_response.json()
        print("Treatment data:", treatment_data)  # Debugging output
        if isinstance(treatment_data, list) and len(treatment_data) > 0:
            treatment_data = treatment_data[0]
        else:
            print("No treatment data found.")
            return None, None, None, None, None, None, None

        # Fetch profile data
        profile_response = requests.get(f"{NIGHTSCOUT_URL}/api/v1/profile.json")
        profile_data = profile_response.json()

        # The profile data is returned as a list, so we need to access the first item in the list
        if isinstance(profile_data, list) and len(profile_data) > 0:
            profile_data = profile_data[0]  # Access the first dictionary in the list
        else:
            print("Profile data is not in the expected list format.")
            return None, None, None, None, None, None, None

        # Inspect the structure of 'store' and look for available profiles
        if 'store' in profile_data:
            store = profile_data['store']

            # Attempt to find the Default profile, or fall back to the first available profile
            default_profile_key = 'Default' if 'Default' in store else next(iter(store))
        else:
            print("Profile 'store' not found.")
            return None, None, None, None, None, None, None

        # Extract Default profile (or fallback profile)
        profiles = store[default_profile_key]
        default_profile = profiles

        # Ensure carbratio, sens, and basal are present and are lists
        icr = match_profile_to_time(default_profile.get('carbratio', []))
        isf = match_profile_to_time(default_profile.get('sens', []))
        basal_rate = match_profile_to_time(default_profile.get('basal', []))

        if not icr or not isf or not basal_rate:
            print("Could not find necessary profile data.")
            return None, None, None, None, None, None, None

        # Extract relevant data
        current_glucose_mgdl = glucose_data['sgv']
        current_glucose_mmol = current_glucose_mgdl / MGDL_TO_MMOL

        iob = treatment_data.get('insulin', 0)
        if iob is None:
            iob = 0  # Default IOB to 0 if not provided

        # Ensure carbs is not None
        carbs = treatment_data.get('carbs', 0)
        if carbs is None:
            carbs = 0  # Default carbs to 0 if not provided

        # Fetch the timestamp of the last bolus
        last_bolus_time = treatment_data.get('mills', None)  # Get as milliseconds timestamp

        return current_glucose_mmol, iob, carbs, isf, icr, basal_rate, last_bolus_time
    except Exception as e:
        print(f"Error fetching data from Nightscout: {e}")
        return None, None, None, None, None, None, None

# Predict future glucose based on IOB, carbs, and basal rates
def predict_future_glucose(current_glucose_mmol, iob, carbs, isf, icr, basal_rate):
    # Ensure isf and icr are not None (set defaults if necessary)
    if isf is None:
        isf = 1  # Default ISF to 1 to avoid division by None
    if icr is None:
        icr = 1  # Default ICR to 1 to avoid division by None

    # Estimate glucose effect from IOB in mmol/L (over 30 min)
    insulin_effect_mmol = iob * isf / 2  # Insulin effect for next 30 min (half of full ISF effect)

    # Estimate glucose effect from carbs in mmol/L (over 30 min)
    carb_effect_mmol = carbs / icr / 2  # Carbs effect for next 30 min (half of full carb effect)

    # Predict future glucose
    predicted_glucose_mmol = current_glucose_mmol - insulin_effect_mmol + carb_effect_mmol

    return predicted_glucose_mmol

# Calculate proportional temp basal adjustments based on predicted glucose
def calculate_temp_basal(predicted_glucose_mmol, basal_rate):
    if predicted_glucose_mmol > TARGET_BG_HIGH:
        deviation_above = predicted_glucose_mmol - TARGET_BG_HIGH
        proportional_increase = min(MAX_TEMP_BASAL_INCREASE, deviation_above / (TARGET_BG_HIGH - TARGET_BG_LOW) * MAX_TEMP_BASAL_INCREASE)
        temp_basal = basal_rate * (1 + proportional_increase)
        return f"Suggest increasing basal rate by {proportional_increase * 100:.1f}% to {temp_basal:.2f} U/hr for {DURATION} minutes."

    elif predicted_glucose_mmol < TARGET_BG_LOW:
        deviation_below = TARGET_BG_LOW - predicted_glucose_mmol
        proportional_decrease = min(MAX_TEMP_BASAL_DECREASE, deviation_below / (TARGET_BG_HIGH - TARGET_BG_LOW) * MAX_TEMP_BASAL_DECREASE)
        temp_basal = basal_rate * max(0, (1 - proportional_decrease))
        return f"Suggest decreasing basal rate by {proportional_decrease * 100:.1f}% to {temp_basal:.2f} U/hr for {DURATION} minutes."

    else:
        return "Blood glucose is predicted to be within the target range. No temporary basal adjustment needed."

# Calculate bolus or carb advice based on predicted glucose
def calculate_bolus_or_carb(predicted_glucose_mmol, isf, icr, iob, last_bolus_time):
    current_time = datetime.utcnow()

    # Check if last_bolus_time is an integer (timestamp in milliseconds) and convert it to a datetime object
    if isinstance(last_bolus_time, int):
        last_bolus_time = datetime.utcfromtimestamp(last_bolus_time / 1000.0)  # Convert from milliseconds to datetime
    else:
        last_bolus_time = None  # Handle case where no timestamp is provided

    # Check if the grace period after the last bolus has passed
    if last_bolus_time:
        minutes_since_last_bolus = (current_time - last_bolus_time).total_seconds() / 60
    else:
        minutes_since_last_bolus = GRACE_PERIOD_MINUTES + 1  # No bolus, so grace period doesn't apply

    if predicted_glucose_mmol > TARGET_BG_HIGH + 2.0 and minutes_since_last_bolus > GRACE_PERIOD_MINUTES:
        insulin_needed = (predicted_glucose_mmol - TARGET_BG_HIGH) / isf - iob
        insulin_needed = max(0, min(insulin_needed, MAX_BOLUS_LIMIT))  # Apply max bolus limit
        if insulin_needed >= BOLUS_THRESHOLD:
            return f"Consider taking {insulin_needed:.1f} units of insulin to correct high blood glucose."

    elif predicted_glucose_mmol < TARGET_BG_LOW - 1.0:
        carbs_needed = (TARGET_BG_LOW - predicted_glucose_mmol) * icr
        if carbs_needed >= CARB_THRESHOLD:
            return f"Consider consuming {carbs_needed:.1f} grams of carbs to correct low blood glucose."

    return None  # No bolus or carb needed

# Send notification to phone
def send_notification(advice):
    pb = Pushbullet(PUSHBULLET_API_KEY)
    pb.push_note("Treatment Advice", advice)

# Wait until the next 5-minute interval to sync with Nightscout
def wait_for_next_interval():
    current_time = datetime.now()
    seconds_until_next_interval = 300 - (current_time.minute % 5) * 60 - current_time.second
    print(f"Waiting {seconds_until_next_interval} seconds to sync with Nightscout updates...")
    time.sleep(seconds_until_next_interval)

# Main loop
def main():
    # Initial wait to sync with Nightscout updates
    wait_for_next_interval()

    while True:
        current_glucose_mmol, iob, carbs, isf, icr, basal_rate, last_bolus_time = get_nightscout_data()

        if current_glucose_mmol is not None:
            # Predict future glucose using OpenAPS-like logic
            predicted_glucose_mmol = predict_future_glucose(current_glucose_mmol, iob, carbs, isf, icr, basal_rate)

            # Get temp basal advice based on predicted glucose
            temp_basal_advice = calculate_temp_basal(predicted_glucose_mmol, basal_rate)

            # Get bolus or carb advice based on predicted glucose
            bolus_or_carb_advice = calculate_bolus_or_carb(predicted_glucose_mmol, isf, icr, iob, last_bolus_time)

            # Send the appropriate notifications
            if bolus_or_carb_advice:
                send_notification(bolus_or_carb_advice)
            else:
                send_notification(temp_basal_advice)
        else:
            print("Error: Could not fetch necessary data from Nightscout.")

        # Wait until the next 5-minute cycle
        wait_for_next_interval()

if __name__ == "__main__":
    main()