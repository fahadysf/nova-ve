# Browser Station tint2

Browser Station runs tint2 17.1.3 with the Xlib event-queue fix for #305. The
patch drains events Xlib already buffered before tint2 waits on the socket.
The upstream archive contains the license and copyright notices.

The image includes the verified archive, checksum, patch, and build inputs
under `/usr/local/share/nova-ve/tint2-source`. To rebuild in a clean Alpine
3.24 build environment, copy that directory, then run these commands inside it.

```sh
xargs apk add --no-cache < build-dependencies.txt
sh ./build.sh /out/tint2
```

[Alpine tint2 build recipe](https://github.com/alpinelinux/aports/blob/be91f091976245dadc30c72c5fe5c8cda3a1c9e3/community/tint2/APKBUILD)
