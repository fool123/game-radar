import argparse
from pathlib import Path
import threading
import json
from .service import Service
from .server import make_server


def main():
    parser=argparse.ArgumentParser(description='Game Radar local workbench')
    parser.add_argument('command',nargs='?',choices=['serve','collect','report'],default='serve')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--no-send',action='store_true',help='Save the collection snapshot without sending it; use before research enrichment')
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'runtime')
    args=parser.parse_args()
    service=Service(args.data_dir)
    if args.command=='report':
        print(json.dumps({'report':service.report()['id']},ensure_ascii=False))
        return
    if args.command=='collect':
        service.job_lock.acquire()
        service._collect(send_after_collection=not args.no_send)
        print(json.dumps(service.job,ensure_ascii=False))
        if service.job['status']=='failed': raise SystemExit(1)
        return
    http=make_server(service,args.port)
    threading.Thread(target=service.schedule_loop,daemon=True,name='radar-scheduler').start()
    print(f'Game Radar ready: http://127.0.0.1:{http.server_port}/',flush=True)
    try: http.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        service.stop_event.set()
        http.server_close()


if __name__=='__main__': main()
