FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/vpn-api .

FROM alpine:3.24
RUN apk add --no-cache ca-certificates
COPY --from=build /out/vpn-api /usr/local/bin/vpn-api
USER 65532:65532
ENTRYPOINT ["vpn-api"]
