FROM golang:1.27.1 AS build
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src
COPY lib/wow ./lib/wow
COPY modules/character/go.mod modules/character/go.sum ./modules/character/
WORKDIR /src/modules/character
RUN go mod download
COPY modules/character ./
RUN CGO_ENABLED=0 go build -trimpath -o /module ./cmd

FROM scratch
COPY --from=build /module /module
ENTRYPOINT ["/module"]
