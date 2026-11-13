import urllib.request, json
r = json.loads(urllib.request.urlopen("http://localhost:5000/api/sequences").read())
s = r["sequences"]
total = len(s)
with_media = sum(1 for x in s if x["media_exists"])
resolved = sum(1 for x in s if x.get("media_resolved"))
no_media = sum(1 for x in s if not x["media_exists"])
print(f"Total: {total}, With media: {with_media}, Resolved: {resolved}, No media: {no_media}")
for x in s:
    if not x["media_exists"]:
        print(f"  Missing: {x['name']}")
