#!/usr/bin/env python3
"""
Analyze log files to determine the actual token-to-character ratio
from Cerebras API responses.
"""
import json
import os
from pathlib import Path

def count_message_chars(messages):
    """Count characters in user and system messages."""
    total_chars = 0
    for msg in messages:
        role = msg.get('role', '')
        if role not in ('user', 'system'):
            continue

        content = msg.get('content', '')
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and 'text' in part:
                    total_chars += len(part['text'])

    return total_chars

def analyze_log_file(filepath):
    """Analyze a single log file."""
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)

        # Extract request messages
        request_body = data.get('request', {}).get('body', {})
        messages = request_body.get('messages', [])

        # Extract response usage
        response_body = data.get('response', {}).get('body', {})
        usage = response_body.get('usage', {})
        prompt_tokens = usage.get('prompt_tokens', 0)

        if not messages or not prompt_tokens:
            return None

        # Count characters
        char_count = count_message_chars(messages)

        if char_count == 0:
            return None

        # Calculate ratio
        ratio = char_count / prompt_tokens

        return {
            'file': os.path.basename(filepath),
            'chars': char_count,
            'tokens': prompt_tokens,
            'ratio': ratio
        }
    except Exception as e:
        print(f"Error processing {filepath}: {e}")
        return None

def main():
    logs_dir = Path('/home/jan/sources/ProxyV2/cerebras-proxy/demodata/logs')

    results = []

    # Find all JSON log files
    for log_file in logs_dir.rglob('*.json'):
        # Skip SYNTHETIC and ZAI logs, only analyze Cerebras logs
        if '[SYNTHETIC]' in str(log_file) or '[ZAI]' in str(log_file):
            continue

        # Skip non-chat completion requests
        if 'chat_completions' not in str(log_file):
            continue

        result = analyze_log_file(log_file)
        if result:
            results.append(result)

    if not results:
        print("No valid log files found!")
        return

    # Sort by token count
    results.sort(key=lambda x: x['tokens'])

    print(f"\n{'='*80}")
    print(f"Token-to-Character Ratio Analysis ({len(results)} samples)")
    print(f"{'='*80}\n")

    print(f"{'File':<50} {'Chars':>10} {'Tokens':>10} {'Ratio':>10}")
    print(f"{'-'*80}")

    for r in results:
        print(f"{r['file']:<50} {r['chars']:>10,} {r['tokens']:>10,} {r['ratio']:>10.2f}")

    # Calculate statistics
    ratios = [r['ratio'] for r in results]
    avg_ratio = sum(ratios) / len(ratios)
    min_ratio = min(ratios)
    max_ratio = max(ratios)

    print(f"\n{'='*80}")
    print(f"Statistics:")
    print(f"{'='*80}")
    print(f"Average ratio: {avg_ratio:.2f} chars/token")
    print(f"Min ratio:     {min_ratio:.2f} chars/token")
    print(f"Max ratio:     {max_ratio:.2f} chars/token")
    print(f"\nCurrent approximation: 4.00 chars/token (1 token ≈ 4 chars)")
    print(f"Suggested approximation: {avg_ratio:.2f} chars/token (1 token ≈ {avg_ratio:.2f} chars)")
    print(f"Better formula: tokens = chars / {avg_ratio:.1f}")
    print(f"\n")

if __name__ == '__main__':
    main()
