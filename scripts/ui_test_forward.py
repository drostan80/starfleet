"""Temporary TCP forward 0.0.0.0:8096 -> tiny's Jellyfin, for the UI test server only.

The web UI rewrites a link's host to the page's own host (right on tiny, where Sonarr and Jellyfin
share the machine). On the test server the page is on another machine, so this makes
http://<this machine>:8096 reach tiny's Jellyfin and the Jellyfin links work. Stopped with the
server (scripts/ui_test_server.sh stop).
"""

import socket
import threading

TARGET = ("192.168.1.77", 8096)


def pipe(source, sink):
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def main():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 8096))
    server.listen(50)
    while True:
        client, _ = server.accept()
        try:
            upstream = socket.create_connection(TARGET, timeout=10)
        except OSError:
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


if __name__ == "__main__":
    main()
