#!/usr/bin/env python3
"""
Script untuk mengambil data OLT Huawei MA5800 via SNMP
Mengambil 3 metrik: Board Used, PON Port Used, Home Connect
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

OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_HW_BOARD_OPER_STATUS = "1.3.6.1.4.1.2011.6.3.3.2.1.7"
OID_HW_ONT_RUN_STATUS = "1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15"

output_dir = Path("output")
output_dir.mkdir(exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = output_dir / f"olt_snmp_{timestamp}.log"

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
            # Untuk ONT: ambil compound index seperti 0.1.1
            pattern = r'\.(\d+(?:\.\d+)*)$'
            match = re.search(pattern, oid_part)
            index = match.group(1) if match else oid_part.split('.')[-1]
        else:
            # Untuk Interface: ambil hanya angka terakhir
            index = oid_part.split('.')[-1]
        
        value = re.sub(r'^[A-Za-z\-]+:\s*', '', value_part)
        value = value.strip('"').strip()
        
        data[index] = value
    
    return data


def get_olt_model(ip):
    output = run_snmp_command(ip, OID_SYS_DESCR, walk=False)
    if output:
        match = re.search(r'MA5800-[^\s]+', output)
        if match:
            return match.group(0)
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
    try:
        oper_output = run_snmp_command(ip, OID_HW_BOARD_OPER_STATUS)
        
        if oper_output:
            oper_status = parse_snmp_output(oper_output, full_index=False)
            used = sum(1 for status in oper_status.values() if status == "0")
            
            if used > 0:
                logging.info(f"{ip}: Found {used} active boards")
                return {"used": used}
        
        logging.warning(f"{ip}: Could not get board count")
        return {"used": 0}
        
    except Exception as e:
        logging.error(f"Error getting board status for {ip}: {str(e)}")
        return {"used": 0}


def get_pon_port_status(ip):
    try:
        type_output = run_snmp_command(ip, OID_IF_TYPE)
        oper_output = run_snmp_command(ip, OID_IF_OPER_STATUS)
        
        if not type_output or not oper_output:
            logging.warning(f"{ip}: Could not get interface data")
            return {"used": 0}
        
        types = parse_snmp_output(type_output, full_index=False)
        oper_status = parse_snmp_output(oper_output, full_index=False)
        
        used = 0
        total = 0
        
        for idx in types:
            if types[idx] == "250":
                total += 1
                if oper_status.get(idx) == "1":
                    used += 1
        
        logging.info(f"{ip}: Found {total} PON ports, {used} UP")
        return {"used": used}
        
    except Exception as e:
        logging.error(f"Error getting PON port status for {ip}: {str(e)}")
        return {"used": 0}


def get_ont_online_count(ip):
    try:
        ont_output = run_snmp_command(ip, OID_HW_ONT_RUN_STATUS)
        
        if not ont_output:
            return 0
        
        statuses = parse_snmp_output(ont_output, full_index=True)
        online = sum(1 for status in statuses.values() if status == "1")
        
        return online
        
    except Exception as e:
        logging.error(f"Error getting ONT count for {ip}: {str(e)}")
        return 0


def collect_olt_data(ip):
    logging.info(f"Processing OLT: {ip}")
    
    result = {
        "ip": ip,
        "sysname": "",
        "model": "",
        "board_used": 0,
        "pon_port_used": 0,
        "home_connect": 0,
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
        result["board_used"] = get_board_status(ip)["used"]
        result["pon_port_used"] = get_pon_port_status(ip)["used"]
        result["home_connect"] = get_ont_online_count(ip)
        
        result["status"] = "OK"
        logging.info(f"{ip}: OK - {result['sysname']} | Board: {result['board_used']} | PON: {result['pon_port_used']} | ONT: {result['home_connect']}")
        
    except Exception as e:
        result["error"] = str(e)
        logging.error(f"{ip}: Error - {str(e)}")
    
    return result


def main():
    print("=" * 70)
    print("Script SNMP OLT Huawei MA5800")
    print("Monitoring: Board Used, PON Port Used, Home Connect")
    print("=" * 70)
    
    input_file = input("Masukkan nama file text input (contoh: ip_list.txt): ").strip()
    
    if not Path(input_file).exists():
        print(f"Error: File '{input_file}' tidak ditemukan!")
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
    
    base = f"olt_data_{timestamp}"
    csv_file = output_dir / f"{base}.csv"
    json_file = output_dir / f"{base}.json"
    
    df.to_csv(csv_file, index=False)
    df.to_json(json_file, orient='records', indent=2)
    
    print(f"\n✓ CSV: {csv_file}")
    print(f"✓ JSON: {json_file}")
    print(f"✓ Log: {log_file}")
    
    total = len(results)
    success = len([r for r in results if r['status'] == 'OK'])
    total_board = sum([r.get('board_used', 0) for r in results if r['status'] == 'OK'])
    total_pon = sum([r.get('pon_port_used', 0) for r in results if r['status'] == 'OK'])
    total_ont = sum([r.get('home_connect', 0) for r in results if r['status'] == 'OK'])
    
    print(f"\n{'=' * 70}")
    print(f"SUMMARY")
    print(f"{'=' * 70}")
    print(f"Total OLT: {total}")
    print(f"Success: {success}")
    print(f"Failed: {total - success}")
    print(f"{'-' * 70}")
    print(f"Board Used: {total_board}")
    print(f"PON Port Used: {total_pon}")
    print(f"Home Connect: {total_ont}")
    print(f"{'=' * 70}\n")
    
    logging.info(f"Selesai. Success: {success}/{total}")
    logging.info(f"Total - Board: {total_board}, PON: {total_pon}, ONT: {total_ont}")


if __name__ == "__main__":
    main()
