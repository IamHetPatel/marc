import cflib.crtp

if __name__ == '__main__':
    print("Initializing drivers...")
    cflib.crtp.init_drivers()
    
    print("Scanning for Crazyflies (make sure the drone is turned on and the Crazyradio USB dongle is plugged in)...")
    available = cflib.crtp.scan_interfaces()
    
    if len(available) == 0:
        print("No drones found! Check if it's powered on and the USB radio is plugged in.")
    else:
        for i in available:
            print(f"✅ Found drone at URI: {i[0]}")
