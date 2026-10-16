# SSH git server for test_git_ssh_integration.py.
FROM alpine:3.22
RUN apk add --no-cache openssh git \
 && ssh-keygen -A \
 && adduser -D -h /home/git -s /usr/bin/git-shell git \
 && echo 'git:*' | chpasswd -e \
 && mkdir -p /home/git/.ssh \
 && printf 'PasswordAuthentication no\nPubkeyAuthentication yes\nPermitRootLogin no\n' >> /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
