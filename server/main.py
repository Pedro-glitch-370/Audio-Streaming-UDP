from server.socket_server import rodar_server

def main():
    try:
        rodar_server()
    except KeyboardInterrupt:
        print("\nEncerrando o server.")

if __name__ == "__main__":
    main()
