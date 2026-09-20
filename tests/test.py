import os


def t():
    os.chdir("ljkdf")


try:
    t()
except FileNotFoundError as e:
    print(e)
