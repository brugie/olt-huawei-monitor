#!/usr/bin/env python3
"""
Script untuk mengambil data OLT ZTE C600/C620 via SNMP
Mengambil metrik: Board, PON Port, ONT Information
Tested on ZTE C600 V1.2.2
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

# Interface OIDs (untuk PON port)
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
OID_IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"

# ZTE C600 Specific OIDs (base_oid_1: 1.3.6.1.4.1.3902.1082)
OID_ZTE_ONU_STATUS = "1.3.6.1.4.1.3902.1082.500.10.2.3.8.1.4"
OID_ZTE_ONU_NAME = "1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2"

# ONU Status values:
# 1 = logging
# 2 = LOS (Loss of Signal)
# 3 = sync_mib
# 4 = working (online)
# 5 = dying_gasp
# 6 = auth_failed
# 7 = offline

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
            # Untuk ONT: ambil compound index (contoh: 285278465.1)
            pattern = r'\.(\d+\.\d+)$'
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
        # ZTE format: "ZXA10 C600, ZTE ZXA10 Software Version: V1.2.2"
        match = re.search(r'C6[0-2]0', output, re.IGNORECASE)
        if match:
            return f"ZTE-{match.group(0)}"
        match = re.search(r'ZXA10[^\s,]+', output)
        if match:
            return match.group(0)
        if 'ZXA10' in output or 'C600' in output or 'C620' in output:
            return 'ZTE-C600'
    return "Unknown"


def get_olt_sysname(ip):
    output = run_snmp_command(ip, OID_SYS_NAME, walk=False)
    if output:
        parts = output.split('=')
        if len(parts) > 1:
            value = re.sub(r'^[A-Za-z\-]+:\s*', '', parts[1].strip())
            return value.strip('"').strip()
    return "Unknown"


def decode_zte_ifindex(ifindex):
    """
    Decode ZTE ifIndex to shelf/slot/port
    ifIndex encoding untuk ZTE C600:
    - Board1Pon1: 285278465 (0x11010101)
    - Board1Pon2: 285278466 (0x11010102)
    - Board2Pon1: 285278721 (0x11010201)
    Format: shelf.rack.slot.port (1 byte each)
    Byte ketiga (0-255) adalah slot number
    """
    try:
        idx = int(ifindex)
        # Extract slot from byte 3 (counting from right: byte 0,1,2,3)
        # Slot is at position (idx >> 8) & 0xFF
        slot = (idx >> 8) & 0xFF
        return str(slot)
    except:
        return None


def get_board_status(ip):
    """
    Menghitung card/board berdasarkan PON interfaces
    Setiap card memiliki multiple PON ports
    """
    try:
        type_output = run_snmp_command(ip, OID_IF_TYPE)
        admin_output = run_snmp_command(ip, OID_IF_ADMIN_STATUS)
        
        if not type_output:
            logging.warning(f"{ip}: Could not get interface type")
            return {"installed": 0, "used": 0}
        
        types = parse_snmp_output(type_output, full_index=False)
        admin_status = parse_snmp_output(admin_output, full_index=False) if admin_output else {}
        
        # Identifikasi card berdasarkan ifIndex PON port (ifType 250)
        cards = set()
        active_cards = set()
        
        for idx, iftype in types.items():
            if iftype == "250":  # GPON port
                slot = decode_zte_ifindex(idx)
                if slot:
                    cards.add(slot)
                    # Card dianggap active jika ada port yang admin up
                    if admin_status.get(idx) == "1":
                        active_cards.add(slot)
        
        installed = len(cards)
        used = len(active_cards)
        
        if installed > 0:
            logging.info(f"{ip}: Found {installed} cards, {used} active")
            return {"installed": installed, "used": used}
        
        logging.warning(f"{ip}: Could not determine card count")
        return {"installed": 0, "used": 0}
        
    except Exception as e:
        logging.error(f"Error getting card status for {ip}: {str(e)}")
        return {"installed": 0, "used": 0}


def get_pon_port_status(ip):
    """
    Mengambil status PON port dari interface table
    ifType 250 = GPON port
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
        
        for idx, iftype in types.items():
            if iftype == "250":  # GPON port
                installed += 1
                # operStatus 1 = UP
                if oper_status.get(idx) == "1":
                    used += 1
        
        logging.info(f"{ip}: Found {installed} PON ports, {used} UP")
        return {"installed": installed, "used": used}
        
    except Exception as e:
        logging.error(f"Error getting PON port status for {ip}: {str(e)}")
        return {"installed": 0, "used": 0}


def get_ont_status(ip):
    """
    Mengambil status ONT/ONU dari ZTE C600
    ONU Status values:
    - 1 = logging
    - 2 = LOS
    - 3 = sync_mib  
    - 4 = working (online)
    - 5 = dying_gasp
    - 6 = auth_failed
    - 7 = offline
    
    Returns: dict dengan 'installed' (total ONT terdaftar) dan 'online' (ONT yang online)
    """
    try:
        onu_output = run_snmp_command(ip, OID_ZTE_ONU_STATUS)
        
        if not onu_output:
            logging.warning(f"{ip}: Could not get ONU data")
            return {"installed": 0, "online": 0}
        
        statuses = parse_snmp_output(onu_output, full_index=True)
        
        # Total ONU yang terdaftar
        installed = len(statuses)
        
        # ONU yang online: status 1 (logging), 3 (sync_mib), 4 (working)
        online_statuses = ["1", "3", "4"]
        online = sum(1 for status in statuses.values() if status in online_statuses)
        
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
    print("Script SNMP OLT ZTE C600/C620")
    print("Monitoring: Board, PON Port, ONT (Installed/Used/Online)")
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
