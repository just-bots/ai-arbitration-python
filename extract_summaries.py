import json

def get_final_response(log_path):
    with open(log_path, 'r') as f:
        for line in reversed(list(f)):
            data = json.loads(line)
            if data.get('tool_calls'):
                for tc in data['tool_calls']:
                    if tc.get('function', {}).get('name') == 'default_api:send_message':
                        return tc['function']['arguments']
    return "No message found"

print("=== Transactions ===")
print(get_final_response("/Users/blockcenter/.gemini/antigravity/brain/68d89f64-4250-4dd2-be88-5d079060f16f/.system_generated/logs/transcript.jsonl"))
print("\n=== Prosecution ===")
print(get_final_response("/Users/blockcenter/.gemini/antigravity/brain/0dfa6a03-4246-4d2d-b3f1-06e3d6ce8536/.system_generated/logs/transcript.jsonl"))
