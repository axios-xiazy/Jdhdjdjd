import os
import glob
import mmap
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import sys

# --- Heuristic filter for skipping "weird" lines (ASCII art / box-drawing / borders) ---
import unicodedata

_email_re = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
_url_re = re.compile(r'https?://', re.IGNORECASE)

# Unicode blocks to treat as "art"
_WEIRD_RANGES = [
    (0x2500, 0x257F),  # Box Drawing
    (0x2580, 0x259F),  # Block Elements
    (0x25A0, 0x25FF),  # Geometric Shapes
    (0x2800, 0x28FF),  # Braille Patterns
    (0x2300, 0x23FF),  # Misc Technical
]

_border_re = re.compile(r'^[\\s_\\-=*\\u2500-\\u257F\\u2580-\\u259F\\u25A0-\\u25FF\\u2800-\\u28FF]{12,}$')

def _has_weird_block_chars(s: str) -> bool:
    for ch in s:
        cp = ord(ch)
        for lo, hi in _WEIRD_RANGES:
            if lo <= cp <= hi:
                return True
    return False

def _non_ascii_ratio(s: str) -> float:
    if not s:
        return 1.0
    non_ascii = sum(1 for ch in s if ord(ch) > 127)
    return non_ascii / max(1, len(s))

def is_weird_line(s: str) -> bool:
    # Keep immediately if it clearly looks like an account line
    if _email_re.search(s) or _url_re.search(s) or ':' in s:
        return False
    # Borders / long separators
    if _border_re.match(s):
        return True
    # Explicit box/braille/block chars
    if _has_weird_block_chars(s):
        return True
    # High ratio of non-ASCII AND long enough → likely art
    if len(s) > 20 and _non_ascii_ratio(s) > 0.50:
        return True
    return False
# --- End heuristic ---

def process_chunk_termux(args):
    """ประมวลผลข้อมูลเป็นชุดด้วย bytes.find() สำหรับ Termux"""
    file_path, domain_bytes, start, end, format_type = args
    lines = []
    
    try:
        with open(file_path, 'rb') as f:
            # ใช้ mmap แบบ read-only เพื่อหลีกเลี่ยงปัญหาบน Termux
            try:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            except:
                # ถ้า mmap ไม่ทำงาน ให้อ่านไฟล์แบบปกติ
                f.seek(start)
                chunk = f.read(end - start)
                pos = 0
                while True:
                    found = chunk.find(domain_bytes, pos)
                    if found == -1:
                        break
                    
                    line_start = chunk.rfind(b'\n', 0, found) + 1
                    line_end = chunk.find(b'\n', found)
                    line_end = line_end if line_end != -1 else len(chunk)
                    
                    line_bytes = chunk[line_start:line_end]
                    line = line_bytes.decode('utf-8', errors='ignore').strip()
                    
                    if line:
                    if is_weird_line(line):
                        continue
            if is_weird_line(line):
                continue
                        parts = line.split(':')
                        if len(parts) >= 3:
                            if format_type == 1:
                                formatted_line = line
                            else:
                                if 'http' in parts[0] or 'https' in parts[0]:
                                    formatted_line = ':'.join(parts[2:])
                                else:
                                    formatted_line = ':'.join(parts[1:])
                            lines.append(formatted_line)
                    pos = line_end + 1
                return lines
            
            # จัดการการตัด Chunk ให้ตรงกับบรรทัด
            if start > 0:
                mm.seek(start - 1)
                if mm.read(1) != b'\n':
                    prev_newline = mm.rfind(b'\n', 0, start)
                    if prev_newline != -1:
                        start = prev_newline + 1
            
            # หาจุดสิ้นสุดของ Chunk
            mm.seek(end)
            next_newline = mm.find(b'\n', end)
            end = next_newline + 1 if next_newline != -1 else mm.size()
            
            chunk = mm[start:end]
            pos = 0
            while True:
                found = chunk.find(domain_bytes, pos)
                if found == -1:
                    break
                
                line_start = chunk.rfind(b'\n', 0, found) + 1
                line_end = chunk.find(b'\n', found)
                line_end = line_end if line_end != -1 else len(chunk)
                
                line_bytes = chunk[line_start:line_end]
                line = line_bytes.decode('utf-8', errors='ignore').strip()
                
                if line:
            if is_weird_line(line):
                continue
                    parts = line.split(':')
                    if len(parts) >= 3:
                        if format_type == 1:
                            formatted_line = line
                        else:
                            if 'http' in parts[0] or 'https' in parts[0]:
                                formatted_line = ':'.join(parts[2:])
                            else:
                                formatted_line = ':'.join(parts[1:])
                        lines.append(formatted_line)
                pos = line_end + 1
            mm.close()
    except Exception as e:
        print(f"Error processing chunk: {e}")
        pass
    return lines

def process_files_termux(files, domain, format_type, progress_callback=None):
    """ประมวลผลหลายไฟล์สำหรับ Termux ใช้ ThreadPoolExecutor แทน multiprocessing"""
    # ใช้ thread แทน process เพื่อหลีกเลี่ยงปัญหาบน Termux
    max_workers = min(4, len(files))  # จำกัดจำนวน thread
    chunk_size = 8 * 1024 * 1024  # ลดขนาด chunk เป็น 8MB สำหรับ Termux
    domain_bytes = domain.encode('utf-8')
    
    chunks = []
    for file_path in files:
        try:
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                continue
            # แบ่ง chunk ขนาดเล็กลงสำหรับ Termux
            for start in range(0, file_size, chunk_size):
                end = min(start + chunk_size, file_size)
                chunks.append((file_path, domain_bytes, start, end, format_type))
        except Exception as e:
            print(f"Error reading file {file_path}: {e}")
            continue

    unique_lines = set()
    processed_chunks = 0
    
    print(f"Processing {len(chunks)} chunks with {max_workers} threads...")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_chunk = {executor.submit(process_chunk_termux, chunk): chunk for chunk in chunks}
        
        for future in future_to_chunk:
            try:
                result = future.result(timeout=30)  # timeout 30 วินาที
                unique_lines.update(result)
                processed_chunks += 1
                
                if progress_callback:
                    progress_callback(processed_chunks, len(chunks))
                else:
                    print(f"Processed {processed_chunks}/{len(chunks)} chunks, found {len(unique_lines)} unique lines")
                    
            except Exception as e:
                print(f"Error processing chunk: {e}")
                processed_chunks += 1
    
    return unique_lines

def find_accounts_termux(domain, format_type, folder_path='log', output_dir='result', progress_callback=None):
    """
    Find accounts matching the given domain - Termux compatible version
    Returns: (message, count, duration, output_file_path)
    """
    OUTPUT_FILE = os.path.join(output_dir, f'Log Account By.zxvxzv [ {domain} ].txt')

    try:
        # สร้างโฟลเดอร์ result ถ้าไม่มี
        os.makedirs(output_dir, exist_ok=True)
        
        # ตรวจสอบโฟลเดอร์ log
        if not os.path.exists(folder_path):
            return f"Folder '{folder_path}' not found!", 0, 0, None

        print("🔍 Scanning files...")
        files = glob.glob(os.path.join(folder_path, '*.txt'))
        if not files:
            return f"No .txt files found in '{folder_path}' folder!", 0, 0, None

        print(f"Found {len(files)} files to process")
        
        start_time = time.time()
        unique_lines = process_files_termux(files, domain, format_type, progress_callback)

        if unique_lines:
            # เขียนไฟล์ผลลัพธ์
            print(f"Writing results to {OUTPUT_FILE}")
            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                for line in sorted(unique_lines):  # เรียงลำดับผลลัพธ์
                    f.write(f"{line}\n")
            
            end_time = time.time()
            duration = end_time - start_time
            
            message = f"✨ Done! Found {len(unique_lines):,} unique lines in {duration:.2f}s"
            print(message)
            print(f"📁 Results saved to: {OUTPUT_FILE}")
            return message, len(unique_lines), duration, OUTPUT_FILE
        else:
            return "❌ No matching data found!", 0, 0, None

    except Exception as e:
        error_msg = f"❌ Error: {e}"
        print(error_msg)
        return error_msg, 0, 0, None

def main():
    """Main function สำหรับ command line interface"""
    print("=" * 50)
    print("🔍 Log Account Finder - Termux Compatible Version")
    print("=" * 50)
    
    # ตรวจสอบโฟลเดอร์ log
    if not os.path.exists('log'):
        print("❌ Error: 'log' folder not found!")
        print("Please create a 'log' folder and put your .txt files there.")
        return
    
    # รับ input จากผู้ใช้
    domain = input("Domain to find: ").strip()
    if not domain:
        print("❌ Error: Domain cannot be empty!")
        return
    
    print("\nSelect output format:")
    print("1. url:email:pass (full format)")
    print("2. email:pass (email and password only)")
    
    try:
        format_type = int(input("Choose (1 or 2): ").strip())
        if format_type not in [1, 2]:
            print("❌ Error: Please choose 1 or 2")
            return
    except ValueError:
        print("❌ Error: Please enter a valid number")
        return
    
    print(f"\n🚀 Starting search for domain: {domain}")
    print(f"📋 Output format: {'url:email:pass' if format_type == 1 else 'email:pass'}")
    print("-" * 50)
    
    # เริ่มการค้นหา
    message, count, duration, output_file = find_accounts_termux(domain, format_type)
    
    print("-" * 50)
    print(message)
    if output_file and os.path.exists(output_file):
        print(f"📁 File saved: {output_file}")
        print(f"📊 File size: {os.path.getsize(output_file)} bytes")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n❌ Operation cancelled by user")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        print("Please check your files and try again.")

