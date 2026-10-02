f = '/usr/local/lib/python3.10/dist-packages/lk21/extractors/bypasser.py'
c = open(f).read()
old = 'removeprefix(urlparse(url).netloc, "www."))'
new = 'removeprefix((lambda u: urlparse(u).netloc if "[" not in u else "")(url), "www."))'
open(f, 'w').write(c.replace(old, new))
print('lk21 patched:', old in c)
