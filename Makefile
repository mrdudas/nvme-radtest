CFLAGS ?= -O2 -Wall -Wextra -g

bin/nvblk: src/nvblk.c
	mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -rf bin
