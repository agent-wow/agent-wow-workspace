FROM golang:1.27.1 AS build
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src
COPY lib/wow ./lib/wow
COPY modules/inventory/go.mod modules/inventory/go.sum ./modules/inventory/
WORKDIR /src/modules/inventory
RUN go mod download
COPY modules/inventory ./
RUN CGO_ENABLED=0 go build -trimpath -o /module ./cmd

FROM scratch
COPY --from=build /module /module
ENTRYPOINT ["/module"]
