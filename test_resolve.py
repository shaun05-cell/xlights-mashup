import urllib.request, json

resp = urllib.request.urlopen("http://localhost:5000/api/sequences")
data = json.loads(resp.read())
seqs = data["sequences"]

total = len(seqs)
with_media = sum(1 for s in seqs if s["media_exists"])
resolved = sum(1 for s in seqs if s.get("media_resolved"))
no_media = sum(1 for s in seqs if not s["media_exists"])

print(f"Total sequences: {total}")
print(f"  Media found directly: {with_media - resolved}")
print(f"  Media resolved (fuzzy): {resolved}")
print(f"  Still no media: {no_media}")

print(f"\nResolved examples:")
for s in seqs:
    if s.get("media_resolved"):
        print(f"  {s['name'][:45]:45s}")
        print(f"    Original: {s['media_file_original']}")
        print(f"    Resolved: {s['media_file']}")
        break

print(f"\nStill missing:")
for s in seqs:
    if not s["media_exists"]:
        print(f"  {s['name'][:45]:45s} -> {s['media_file']}")
