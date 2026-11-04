
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "WarehouseMapLoader.generated.h"

USTRUCT(BlueprintType)
struct FWarehouseNode
{
    GENERATED_BODY()

    UPROPERTY(BlueprintReadOnly)
    FString Id;

    UPROPERTY(BlueprintReadOnly)
    FVector Position = FVector::ZeroVector;
};

USTRUCT(BlueprintType)
struct FWarehouseEdge
{
    GENERATED_BODY()

    UPROPERTY(BlueprintReadOnly)
    FString From;

    UPROPERTY(BlueprintReadOnly)
    FString To;
};

UCLASS()
class AGVSIMULATION_API AWarehouseMapLoader : public AActor
{
    GENERATED_BODY()

public:
    AWarehouseMapLoader();

protected:
    virtual void BeginPlay() override;

public:
    UFUNCTION(BlueprintCallable, Category = "Warehouse")
    bool LoadMap();

    UPROPERTY(BlueprintReadOnly, Category = "Warehouse")
    TArray<FWarehouseNode> Nodes;

    UPROPERTY(BlueprintReadOnly, Category = "Warehouse")
    TArray<FWarehouseEdge> Edges;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Warehouse")
    TSubclassOf<AActor> NodeMarkerClass;
};
