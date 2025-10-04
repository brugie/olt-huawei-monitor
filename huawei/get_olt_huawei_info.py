
#!/usr/bin/env python3
"""
Script untuk mengambil data OLT Huawei MA5800 via SNMP
Mengambil 5 metrik: Board, PON Port, ONT (Installed/Used/Online)
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

output_dir = Path("output") / datetime.now().strftime("%Y-%m-%d")
output_dir.mkdir(parents=True, exist_ok=True)

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
            
            # Hitung installed (semua board yang ada, termasuk offline)
            # Status: 0=normal, 1=fault, 2=offline
            # Installed = yang bukan empty/not-present
            installed = len(oper_status)
            
            # Hitung used (hanya yang status normal/active)
            used = sum(1 for status in oper_status.values() if status == "0")
            
            if installed > 0:
                logging.info(f"{ip}: Found {installed} installed boards, {used} active")
                return {"installed": installed, "used": used}
        
        logging.warning(f"{ip}: Could not get board count")
        return {"installed": 0, "used": 0}
        
    except Exception as e:
        logging.error(f"Error getting board status for {ip}: {str(e)}")
        return {"installed": 0, "used": 0}


def get_pon_port_status(ip):
    """
    Mengambil status PON port
    Returns: dict dengan 'installed' (total PON port) dan 'used' (PON port yang UP)
    """
    try:
        type_output = run_snmp_command(ip, OID_IF_TYPE)
        oper_output = run_snmp_command(ip, OID_IF_OPER_STATUS)
        
        if not type_output or not oper_output:
            logging.warning(f"{ip}: Could not get interface data")
            return {"installed": 0, "used": 0}
        
        types = parse_snmp_output(type_output, full_index=False)
        oper_status = parse_snmp_output(oper_output, full_index=False)
        
        installed = 0
        used = 0
        
        for idx in types:
            # ifType 250 = GPON port
            if types[idx] == "250":
                installed += 1
                # operStatus 1 = UP
                if oper_status.get(idx) == "1":
                    used += 1
        
        logging.info(f"{ip}: Found {installed} PON ports installed, {used} UP")
        return {"installed": installed, "used": used}
        
    except Exception as e:
        logging.error(f"Error getting PON port status for {ip}: {str(e)}")
        return {"installed": 0, "used": 0}


def get_ont_status(ip):
    """
    Mengambil status ONT
    Returns: dict dengan 'installed' (total ONT terdaftar) dan 'online' (ONT yang online)
    ONT Run Status: 0=offline, 1=online
    """
    try:
        ont_output = run_snmp_command(ip, OID_HW_ONT_RUN_STATUS)
        
        if not ont_output:
            logging.warning(f"{ip}: Could not get ONT data")
            return {"installed": 0, "online": 0}
        
        statuses = parse_snmp_output(ont_output, full_index=True)
        
        # Total ONT yang terdaftar (installed)
        installed = len(statuses)
        
        # ONT yang online (status = 1)
        online = sum(1 for status in statuses.values() if status == "1")
        
        logging.info(f"{ip}: Found {installed} ONT installed, {online} online")
        return {"installed": installed, "online": online}
        
    except Exception as e:
        logging.error(f"Error getting ONT status for {ip}: {str(e)}")
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
        "ont_installed": 0,
        "ont_online": 0,
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
        
        ont_status = get_ont_status(ip)
        result["ont_installed"] = ont_status["installed"]
        result["ont_online"] = ont_status["online"]
        
        result["status"] = "OK"
        logging.info(f"{ip}: OK - {result['sysname']} | Board: {result['board_used']}/{result['board_installed']} | PON: {result['pon_port_used']}/{result['pon_port_installed']} | ONT: {result['ont_online']}/{result['ont_installed']}")
        
    except Exception as e:
        result["error"] = str(e)
        logging.error(f"{ip}: Error - {str(e)}")
    
    return result


def main():
    print("=" * 70)
    print("Script SNMP OLT Huawei MA5800")
    print("Monitoring: Board, PON Port, ONT (Installed/Used/Online)")
    print("=" * 70)
    
    # File input
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
    total_board_installed = sum([r.get('board_installed', 0) for r in results if r['status'] == 'OK'])
    total_board_used = sum([r.get('board_used', 0) for r in results if r['status'] == 'OK'])
    total_pon_installed = sum([r.get('pon_port_installed', 0) for r in results if r['status'] == 'OK'])
    total_pon_used = sum([r.get('pon_port_used', 0) for r in results if r['status'] == 'OK'])
    total_ont_installed = sum([r.get('ont_installed', 0) for r in results if r['status'] == 'OK'])
    total_ont_online = sum([r.get('ont_online', 0) for r in results if r['status'] == 'OK'])
    
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
    print(f"ONT Installed: {total_ont_installed}")
    print(f"ONT Online: {total_ont_online}")
    print(f"{'=' * 70}\n")
    
    logging.info(f"Selesai. Success: {success}/{total}")
    logging.info(f"Total - Board: {total_board_used}/{total_board_installed}, PON: {total_pon_used}/{total_pon_installed}, ONT: {total_ont_online}/{total_ont_installed}")


if __name__ == "__main__":
    main()
