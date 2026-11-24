"""CDU 한 공장만 켠 로컬 시연 설정 (D0 호환 진입점). 다섯 공장은 scripts/prepare_twin.py 를 쓴다."""
from scripts.prepare_twin import prepare


def main():
    print(prepare(["cdu"], username="cdu-d4-demo"))


if __name__ == "__main__":
    main()
