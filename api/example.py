from api import TopologyPlatformClient
import json

def main():
    client = TopologyPlatformClient()
    client.login("xxx", "xxx")

    with open("./input_path", "r", encoding="utf-8") as f:
        obs = json.load(f)
    payload = client.formatData(obs, "projectId", "networkId")

    with open("./output_path", "w", encoding="utf-8") as f:
        json.dump(payload, f)

    client.import_topology(payload)


if __name__ == "__main__":
    main()