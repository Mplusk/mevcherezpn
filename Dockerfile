FROM alpine:3.24
RUN apk add --no-cache ca-certificates
COPY --chmod=0755 vpn-api /usr/local/bin/vpn-api
USER 65532:65532
ENTRYPOINT ["vpn-api"]
