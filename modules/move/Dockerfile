FROM golang:1.27.1 AS build
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src
COPY lib/wow ./lib/wow
COPY modules/move/go.mod modules/move/go.sum ./modules/move/
WORKDIR /src/modules/move
RUN go mod download
COPY modules/move ./
RUN CGO_ENABLED=0 go build -trimpath -o /module ./cmd

FROM scratch
COPY --from=build /module /module
ENTRYPOINT ["/module"]
