FROM golang:1.27.1 AS build
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src/modules/example
COPY modules/example/go.mod modules/example/go.sum ./
RUN go mod download
COPY modules/example ./
RUN CGO_ENABLED=0 go build -trimpath -o /module ./cmd

FROM scratch
COPY --from=build /module /module
ENTRYPOINT ["/module"]
