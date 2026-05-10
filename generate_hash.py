# generate_hash.py
from werkzeug.security import generate_password_hash
import sys

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python generate_hash.py <원하는_비밀번호>")
        sys.exit(1)
    
    password = sys.argv[1]
    hashed_password = generate_password_hash(password)
    print(f"입력한 비밀번호 '{password}'의 해시값:")
    print(hashed_password)
    print("\n이 해시값을 .env 파일의 ADMIN_PASSWORD_HASH에 붙여넣으세요.")

