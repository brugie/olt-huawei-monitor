#!/usr/bin/env python3
"""
Script untuk mengambil data OLT Fiberhome AN6000-17 via SNMP
Mengambil metrik: Board, PON Port, ONU Information
"""

import pandas as pd
import subprocess
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import logging

SNMP_COMMUNITY = "public"
SNMP_VERSION = "2c"
SNMP_TIMEOUT = 30
MAX_WORKERS = 5

# Standard OIDs
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"

# Fiberhome specific OIDs (Enterprise: 5875)
OID_FH_CARD_STATUS = "1.3.6.1.4.1.5875.800.3.9.2.1.1.5"
OID_FH_CARD_TYPE = "1.3.6.1.4.1.5875.800.3.9.2.1.1.2"
OID_FH_PON_PORT_TYPE = "1.3.6.1.4.1.5875.800.3.9.3.4.1.1"
OID_FH_PON_PORT_NAME = "1.3.6.1.4.1.5875.800.3.9.3.4.1.2"
OID_FH_ONU_STATUS = "1.3.6.1.4.1.5875.800.3.10.1.1.11"

output_dir = Path("output") / datetime.now().strftime("%Y-%m-%d")
output_dir.mkdir(parents=True, exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = output_dir / f"olt_fiberhome_{timestamp}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)


def run_snmp_command(ip, oid, walk=True):
    try:
        cmd = [
            "snmpwalk" if walk else "snmpget",
            "-v", SNMP_VERSION,
            "-c", SNMP_COMMUNITY,
            "-t", str(SNMP_TIMEOUT),
            "-r", "2",
            ip,
            oid
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SNMP_TIMEOUT + 10
        )
        
        return result.stdout if result.returncode == 0 else None
            
    except:
        return None


def parse_snmp_output(output, full_index=False):
    if not output:
        return {}
    
    data = {}
    lines = output.strip().split('\n')
    
    for line in lines:
        if '=' not in line:
            continue
            
        parts = line.split('=', 1)
        if len(parts) != 2:
            continue
            
        oid_part = parts[0].strip()
        value_part = parts[1].strip()
        
        if full_index:
            pattern = r'\.(\d+(?:\.\d+)*)$'
            match = re.search(pattern, oid_part)
            index = match.group(1) if match else oid_part.split('.')[-1]
        else:
            index = oid_part.split('.')[-1]
        
        value = re.sub(r'^[A-Za-z\-]+:\s*', '', value_part)
        value = value.strip('"').strip()
        
        data[index] = value
    
    return data


def get_olt_model(ip):
    output = run_snmp_command(ip, OID_SYS_DESCR, walk=False)
    if output:
        match = re.search(r'AN6000[^\s,]*', output, re.IGNORECASE)
        if match:
            return match.group(0)
        if 'fiberhome' in output.lower():
            parts = output.split('=')
            if len(parts) > 1:
                return parts[1].strip().split()[0]
    return "Unknown"


def get_olt_sysname(ip):
    output = run_snmp_command(ip, OID_SYS_NAME, walk=False)
    if output:
        parts = output.split('=')
        if len(parts) > 1:
            value = re.sub(r'^[A-Za-z\-]+:\s*', '', parts[1].strip())
            return value.strip('"').strip()
    return "Unknown"


def get_board_status(ip):
    """
    Mengambil status board dari Fiberhome
    Card Status: 1 = active/normal
    """
    try:
        status_output = run_snmp_command(ip, OID_FH_CARD_STATUS)
        
        if status_output:
            statuses = parse_snmp_output(status_output, full_index=False)
            installed = len(statuses)
            used = len([v for v in statuses.values() if v == "1"])
            
            if installed > 0:
                logging.info(f"{ip}: Found {installed} cards installed, {used} active")
                return {"installed": installed, "used": used}
        
        type_output = run_snmp_command(ip, OID_FH_CARD_TYPE)
        if type_output:
            types = parse_snmp_output(type_output, full_index=False)
            installed = len(types)
            used = installed
            
            logging.info(f"{ip}: Found {installed} cards (from type info)")
            return {"installed": installed, "used": used}
        
        logging.warning(f"{ip}: Could not get board count")
        return {"installed": 0, "used": 0}
        
    except Exception as e:
        logging.error(f"Error getting board status for {ip}: {str(e)}")
        return {"installed": 0, "used": 0}


def get_pon_port_status(ip):
    """
    Mengambil status PON port dari Fiberhome
    Returns: dict dengan 'installed' dan 'used'
    """
    try:
        type_output = run_snmp_command(ip, OID_FH_PON_PORT_TYPE)
        
        if not type_output:
            logging.warning(f"{ip}: Could not get PON port data")
            return {"installed": 0, "used": 0}
        
        types = parse_snmp_output(type_output, full_index=False)
        installed = len([v for v in types.values() if v == "1"])
        
        name_output = run_snmp_command(ip, OID_FH_PON_PORT_NAME)
        
        if name_output:
            names = parse_snmp_output(name_output, full_index=False)
            used = len([v for v in names.values() if v and "PON" in v.upper()])
        else:
            used = installed
        
        logging.info(f"{ip}: Found {installed} PON ports installed, {used} configured")
        return {"installed": installed, "used": used}
        
    except Exception as e:
        logging.error(f"Error getting PON port status for {ip}: {str(e)}")
        return {"installed": 0, "used": 0}


def get_onu_status(ip):
    """
    Mengambil status ONU dari Fiberhome
    ONU Status: 0=deregistered, 1=online, 2=offline, 3=unknown
    """
    try:
        onu_status_output = run_snmp_command(ip, OID_FH_ONU_STATUS)
        
        if not onu_status_output:
            logging.warning(f"{ip}: Could not get ONU data")
            return {"installed": 0, "online": 0}
        
        statuses = parse_snmp_output(onu_status_output, full_index=False)
        installed = len([v for v in statuses.values() if v != "0"])
        online = len([v for v in statuses.values() if v == "1"])
        
        logging.info(f"{ip}: Found {installed} ONU installed, {online} online")
        return {"installed": installed, "online": online}
        
    except Exception as e:
        logging.error(f"Error getting ONU status for {ip}: {str(e)}")
        return {"installed": 0, "online": 0}


def collect_olt_data(ip):
    logging.info(f"Processing OLT: {ip}")
    
    result = {
        "ip": ip,
        "sysname": "",
        "model": "",
        "board_installed": 0,
        "board_used": 0,
        "pon_port_installed": 0,
        "pon_port_used": 0,
        "onu_installed": 0,
        "onu_online": 0,
        "status": "error",
        "error": ""
    }
    
    try:
        test = run_snmp_command(ip, OID_SYS_DESCR, walk=False)
        if not test:
            result["error"] = "timeout/unreachable"
            logging.warning(f"{ip}: Timeout/Unreachable")
            return result
        
        result["sysname"] = get_olt_sysname(ip)
        result["model"] = get_olt_model(ip)
        
        board_status = get_board_status(ip)
        result["board_installed"] = board_status["installed"]
        result["board_used"] = board_status["used"]
        
        pon_status = get_pon_port_status(ip)
        result["pon_port_installed"] = pon_status["installed"]
        result["pon_port_used"] = pon_status["used"]
        
        onu_status = get_onu_status(ip)
        result["onu_installed"] = onu_status["installed"]
        result["onu_online"] = onu_status["online"]
        
        result["status"] = "OK"
        logging.info(f"{ip}: OK - {result['sysname']} | Board: {result['board_used']}/{result['board_installed']} | PON: {result['pon_port_used']}/{result['pon_port_installed']} | ONU: {result['onu_online']}/{result['onu_installed']}")
        
    except Exception as e:
        result["error"] = str(e)
        logging.error(f"{ip}: Error - {str(e)}")
    
    return result


def main():
    print("=" * 70)
    print("Script SNMP OLT Fiberhome AN6000-17")
    print("Monitoring: Board, PON Port, ONU (Installed/Used/Online)")
    print("=" * 70)
    
    input_file = Path("olt.txt")
    
    if not input_file.exists():
        print(f"Error: File '{input_file}' tidak ditemukan!")
        print(f"Silakan buat file 'olt.txt' di folder yang sama dengan script ini.")
        print(f"Format: satu IP per baris")
        return
    
    try:
        with open(input_file, 'r') as f:
            ip_list = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        ip_list = list(set(ip_list))
    except Exception as e:
        print(f"Error membaca file: {str(e)}")
        return
    
    if not ip_list:
        print("Error: Tidak ada IP valid!")
        return
    
    print(f"\nTotal IP: {len(ip_list)}")
    print(f"Max workers: {MAX_WORKERS}")
    print(f"SNMP Community: {SNMP_COMMUNITY}")
    print(f"Timeout: {SNMP_TIMEOUT}s\n")
    
    logging.info(f"Memulai proses untuk {len(ip_list)} IP")
    
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ip = {executor.submit(collect_olt_data, ip): ip for ip in ip_list}
        
        for future in as_completed(future_to_ip):
            try:
                results.append(future.result())
            except Exception as e:
                ip = future_to_ip[future]
                logging.error(f"Exception untuk {ip}: {str(e)}")
                results.append({"ip": ip, "status": "error", "error": str(e)})
    
    df = pd.DataFrame(results).sort_values('ip').reset_index(drop=True)
    
    base = f"olt_fiberhome_{timestamp}"
    csv_file = output_dir / f"{base}.csv"
    json_file = output_dir / f"{base}.json"
    
    df.to_csv(csv_file, index=False)
    df.to_json(json_file, orient='records', indent=2)
    
    print(f"\nCSV: {csv_file}")
    print(f"JSON: {json_file}")
    print(f"Log: {log_file}")
    
    total = len(results)
    success = len([r for r in results if r['status'] == 'OK'])
    total_board_installed = sum([r.get('board_installed', 0) for r in results if r['status'] == 'OK'])
    total_board_used = sum([r.get('board_used', 0) for r in results if r['status'] == 'OK'])
    total_pon_installed = sum([r.get('pon_port_installed', 0) for r in results if r['status'] == 'OK'])
    total_pon_used = sum([r.get('pon_port_used', 0) for r in results if r['status'] == 'OK'])
    total_onu_installed = sum([r.get('onu_installed', 0) for r in results if r['status'] == 'OK'])
    total_onu_online = sum([r.get('onu_online', 0) for r in results if r['status'] == 'OK'])
    
    print(f"\n{'=' * 70}")
    print(f"SUMMARY")
    print(f"{'=' * 70}")
    print(f"Total OLT: {total}")
    print(f"Success: {success}")
    print(f"Failed: {total - success}")
    print(f"{'-' * 70}")
    print(f"Board Installed: {total_board_installed}")
    print(f"Board Used: {total_board_used}")
    print(f"PON Port Installed: {total_pon_installed}")
    print(f"PON Port Used: {total_pon_used}")
    print(f"ONU Installed: {total_onu_installed}")
    print(f"ONU Online: {total_onu_online}")
    print(f"{'=' * 70}\n")
    
    logging.info(f"Selesai. Success: {success}/{total}")
    logging.info(f"Total - Board: {total_board_used}/{total_board_installed}, PON: {total_pon_used}/{total_pon_installed}, ONU: {total_onu_online}/{total_onu_installed}")


if __name__ == "__main__":
    main()
