"""Combine Take 5 deez targeted iMessage CSV exports into an updateable dataset.

Usage: python take5_process.py [message_csv] [body_csv] [output_folder]

Only inputs exported from Chat ID 1731 should be provided. The original
Mac chat.db is not needed. The outputs contain private message text.
"""
from __future__ import annotations
import csv
import json
import re
import sys
from pathlib import Path
from collections import Counter, defaultdict

MEMBER_IDS = {'0': 'Jay', '4': 'Nikhil', '5': 'Amar', '15': 'Nathan',
              '17': 'Shil', '121': 'Anoop', '214': 'Neal', '217': 'Purav'}
REACTION_TYPES = {'2000': 'love', '2001': 'like', '2002': 'dislike',
                  '2003': 'haha', '2004': 'emphasize', '2005': 'question',
                  '2006': 'other_tapback'}
PREFIX = b'\x84\x01+'


def decode_attributed_body(hex_value: str) -> str:
    """Decode the NSString payload in common Apple NSAttributedString typedstreams.

    Validates the typedstream marker and the following range marker instead of
    extracting printable substrings that can accidentally include metadata.
    """
    binary = bytes.fromhex(hex_value)
    pos = binary.index(PREFIX) + len(PREFIX)
    lead = binary[pos]
    if lead < 0x80:
        byte_count, start = lead, pos + 1
    elif lead == 0x81:
        byte_count, start = int.from_bytes(binary[pos + 1:pos + 3], 'little'), pos + 3
    elif lead == 0x82:
        byte_count, start = int.from_bytes(binary[pos + 1:pos + 5], 'little'), pos + 5
    else:
        raise ValueError(f'Unsupported NSString length marker 0x{lead:02x}')
    end = start + byte_count
    if end > len(binary) or not binary[end:end + 2] == b'\x86\x84':
        raise ValueError('Unvalidated attributed-string boundary')
    return binary[start:end].decode('utf-8', errors='strict')


def load_csv(path):
    with open(path, 'r', newline='', encoding='utf-8-sig') as fd:
        return list(csv.DictReader(fd))


def main():
    base_path = Path(sys.argv[1]) if len(sys.argv)>1 else Path('/mnt/data/take5_messages.csv')
    body_path = Path(sys.argv[2]) if len(sys.argv)>2 else Path('/mnt/data/take5_message_bodies.csv')
    dest = Path(sys.argv[3]) if len(sys.argv)>3 else Path('/mnt/data')
    dest.mkdir(parents=True, exist_ok=True)

    original = load_csv(base_path)
    encoded = load_csv(body_path)
    decoded = {}
    for entry in encoded:
        ident = entry['message_id']
        if ident in decoded:
            raise ValueError(f'Duplicate attributed body for {ident}')
        decoded[ident] = decode_attributed_body(entry['attributed_body_hex'])

    normal = {r['message_guid']:r for r in original if r['reaction_type'] == '0'}
    known_ids = {r['message_id'] for r in original if r['reaction_type'] == '0'}
    if not set(decoded).issubset(known_ids):
        raise ValueError('Some body records are not in the selected chat export')

    enriched=[]
    quality=Counter()
    for entry in original:
        record=entry.copy()
        id_=entry['message_id']
        is_msg=entry['reaction_type']=='0'
        record['year'] = entry['message_date'][:4]
        record['member'] = MEMBER_IDS.get(entry['sender_id'], f'Unmapped ({entry["sender_id"]})')
        record['record_type'] = 'message' if is_msg else ('tapback' if entry['reaction_type'] in REACTION_TYPES else 'other_event')
        if not is_msg:
            record['text_source'] = 'reaction_or_event'
        elif entry['message_text']:
            record['text_source'] = 'original'
        elif id_ in decoded:
            record['message_text'] = decoded[id_]
            record['text_source'] = 'attributedBody'
        else:
            record['text_source'] = 'unavailable'
        record['word_count'] = len(re.findall(r"\b\w+(?:['’]\w+)*\b", record['message_text'])) if is_msg else 0
        record['has_object_placeholder'] = '\ufffc' in record['message_text'] if is_msg else False
        record['reaction_name'] = REACTION_TYPES.get(entry['reaction_type'], '')
        record['reacted_to_message_id'] = ''
        record['reacted_to_member'] = ''
        record['reacted_to_year'] = ''
        record['reaction_linked'] = ''
        if record['record_type'] == 'tapback':
            raw_guid = entry['reacted_to_guid']
            normalized = raw_guid.split('/', 1)[-1] if '/' in raw_guid else raw_guid
            target = normal.get(normalized)
            if target:
                record['reacted_to_message_id'] = target['message_id']
                record['reacted_to_member'] = MEMBER_IDS.get(target['sender_id'], f'Unmapped ({target["sender_id"]})')
                record['reacted_to_year'] = target['message_date'][:4]
                record['reaction_linked'] = 'yes'
                quality['linked_tapbacks'] += 1
            else:
                record['reaction_linked'] = 'no'
                quality['unlinked_tapbacks'] += 1
        if is_msg:
            quality['messages'] += 1
            quality[f'text_source_{record["text_source"]}']+=1
            if record['has_object_placeholder']:
                quality['messages_with_object_placeholder']+=1
            if record['member'].startswith('Unmapped'):
                quality['unmapped_sender_messages']+=1
        enriched.append(record)

    output_csv=dest/'take5_wrapped_enriched.csv'
    fields = list(original[0].keys()) + ['year', 'member', 'record_type', 'text_source', 'word_count',
                 'has_object_placeholder', 'reaction_name', 'reacted_to_message_id',
                 'reacted_to_member', 'reacted_to_year', 'reaction_linked']
    with output_csv.open('w', newline='', encoding='utf-8-sig') as fd:
        writer=csv.DictWriter(fd, fieldnames=fields)
        writer.writeheader()
        writer.writerows(enriched)

    summary={
        'source': {'chat_id': 1731, 'message_records': len(enriched),
                   'first_record': min(r['message_date'] for r in enriched),
                   'last_record': max(r['message_date'] for r in enriched),
                   'known_member_ids': MEMBER_IDS, 'quality': dict(quality)},
        'periods': {}
    }
    periods=sorted({r['year'] for r in enriched})+['all_time_available']
    for period in periods:
        participants={name:{'messages':0,'hahas_received':0,'hahas_per_100_messages':0.0,
                            'hahas_given':0,'reactions_received':0,'dislikes_given':0}
                      for name in MEMBER_IDS.values()}
        records= [r for r in enriched if period=='all_time_available' or r['year']==period]
        for r in records:
            name=r['member']
            if name not in participants: continue
            if r['record_type']=='message':
                participants[name]['messages']+=1
            if r['reaction_name']=='haha':
                participants[name]['hahas_given']+=1
            if r['reaction_name']=='dislike':
                participants[name]['dislikes_given']+=1
        # Reactions are credited to the period in which the underlying message was SENT;
        # this is not necessarily the period when somebody added a tapback.
        for r in enriched:
            recipient=r['reacted_to_member']
            if (r['record_type']=='tapback' and recipient in participants
               and (period=='all_time_available' or r['reacted_to_year']==period)):
                participants[recipient]['reactions_received']+=1
                if r['reaction_name']=='haha':
                    participants[recipient]['hahas_received']+=1
        for record in participants.values():
            record['hahas_per_100_messages']=round(
                100*record['hahas_received']/record['messages'],2) if record['messages'] else 0
        summary['periods'][period]={
            'total_messages_with_mapped_sender':sum(p['messages'] for p in participants.values()),
            'participants': participants}
    output_json=dest/'take5_wrapped_summary.json'
    with output_json.open('w', encoding='utf-8') as fd:
        json.dump(summary,fd,indent=2,ensure_ascii=False)

    print('OUTPUTS', output_csv, output_json)
    print('QUALITY', json.dumps(summary['source'],indent=2))
    for period, payload in summary['periods'].items():
        top = sorted(payload['participants'].items(), key=lambda x:-x[1]['messages'])
        print('PERIOD', period,'messages',payload['total_messages_with_mapped_sender'],
              'textleader',top[0], 'best_haha_rate',max(payload['participants'].items(),key=lambda x:x[1]['hahas_per_100_messages']))

if __name__=='__main__':
    main()
